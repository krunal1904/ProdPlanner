import frappe
from collections import defaultdict
import json
from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry

@frappe.whitelist()
def get_workorder_list(company, warehouses, docname=None, filters=None):
    """
    Fetch Work Orders based on filters, evaluate raw material readiness based on
    Work Order Items and Bin stock, log detailed calculations for debugging,
    and update the Bulk Production Page child table.
    """

    try:
        if not docname:
            return {"status": "error", "message": "Document name missing"}

        filters = filters or {}
        filters.setdefault("status", ["not in", ["Cancelled", "Closed", "Completed"]])

        if company:
            filters["company"] = company

        # Step 1: Fetch Work Orders
        wo_names = frappe.get_all(
            "Work Order",
            filters=filters,
            pluck="name",
            limit_page_length=200000000000000,
            order_by="custom_sales_order desc, creation desc",
        )

        if not wo_names:
            return {"status": "error", "message": "No Work Orders found"}

        if not warehouses:
            return {"status": "error", "message": "No warehouses selected"}

        # Convert from serialized string to list

        if isinstance(warehouses, str):
            warehouses = json.loads(warehouses)


        # Load Bulk Production Page
        parent = frappe.get_doc("Bulk Production", docname)
        parent.work_order_list = []   # Clear existing child rows

        if not warehouses:
            return {"status": "error", "message": "No warehouses selected"}

        # Step 2: Collect component raw material items
        component_items = set()
        work_order_docs = []

        for name in wo_names:
            wo = frappe.get_doc("Work Order", name)
            work_order_docs.append(wo)

            for comp in wo.required_items:    # Child table of RM
                component_items.add(comp.item_code)

        # frappe.log_error("COMPONENT ITEMS FOR STOCK CHECK", json.dumps(list(component_items), indent=2))

        # Step 3: Fetch BIN stock for raw materials
        bins = frappe.get_all(
            "Bin",
            filters={"item_code": ["in", list(component_items)], "warehouse": ["in", warehouses]},
            fields=["item_code", "warehouse", "actual_qty"]
        )

        # frappe.log_error("BIN RECORDS RETURNED", json.dumps(bins, indent=2, default=str))

        # Merge stock by item
        stock_map = defaultdict(float)
        for b in bins:
            stock_map[b.item_code] += float(b.actual_qty or 0)

        # frappe.log_error("STOCK MAP AGGREGATION", json.dumps(stock_map, indent=2, default=str))

        enhanced_list = []

        # Step 4: Process each Work Order
        for wo in work_order_docs:
            remaining_required = max(0, float(wo.qty or 0) - float(wo.produced_qty or 0))
            overall_coverage = 1.0
            comp_debug = []

            for comp in wo.required_items:
                required_remaining = max(0, float(comp.required_qty or 0) - float(comp.consumed_qty or 0))

                src_wh = comp.source_warehouse

                # Fallback to default bulk production setting
                if not src_wh:
                    default_wh = frappe.db.get_single_value("Bulk Production Settings", "source_warehouse")
                    if default_wh:
                        src_wh = default_wh
                    else:
                        comp_debug.append({
                            "item_code": comp.item_code,
                            "reason": "Source warehouse missing and no default defined",
                            "status": "Warehouse Required"
                        })
                        overall_coverage = 0
                        continue

                # Check stock only from determined warehouse
                available = frappe.db.get_value(
                    "Bin",
                    {"item_code": comp.item_code, "warehouse": src_wh},
                    "actual_qty"
                ) or 0

                if required_remaining > 0:
                    comp_coverage = (available / required_remaining) * 100
                    overall_coverage = min(overall_coverage, available / required_remaining)
                else:
                    comp_coverage = 100

                comp_debug.append({
                    "item_code": comp.item_code,
                    "required_qty": float(comp.required_qty or 0),
                    "consumed_qty": float(comp.consumed_qty or 0),
                    "required_remaining": required_remaining,
                    "bin_available": available,
                    "warehouse_used": src_wh,
                    "coverage_percent": comp_coverage
                })

            # -------------------------------
            # MATERIAL TRANSFER CHECK FOR IN PROCESS WORK ORDERS
            # -------------------------------
            if wo.status == "In Process":
                transfer_qty = frappe.db.sql("""
                    SELECT SUM(se.fg_completed_qty)
                    FROM `tabStock Entry` se
                    WHERE se.work_order = %s
                    AND se.stock_entry_type = 'Material Transfer for Manufacture'
                    AND se.docstatus = 1
                """, (wo.name,))[0][0] or 0

                planned_qty = float(wo.qty or 0)

                if transfer_qty >= planned_qty:
                    status = "FULLY TRANSFERRED"
                elif 0 < transfer_qty < planned_qty:
                    status = "PARTIALLY TRANSFERRED"
                else:
                    status = "TRANSFER INITIATED"
            else:
                status = None  # Continue to material readiness logic

            coverage_pct = overall_coverage * 100

            if not status:
                if remaining_required == 0:
                    status = "COMPLETED"
                elif overall_coverage >= 1:
                    status = "READY"
                elif overall_coverage >= 0.5:
                    status = "PARTIAL"
                else:
                    status = "INSUFFICIENT"

            # Log full breakdown for manual validation
            # frappe.log_error(
            #     f"RAW MATERIAL CALCULATION DEBUG - {wo.name}",
            #     json.dumps({
            #         "finished_good": wo.production_item,
            #         "planned_qty": float(wo.qty or 0),
            #         "produced_qty": float(wo.produced_qty or 0),
            #         "remaining_required_fg": remaining_required,
            #         "components": comp_debug,
            #         "overall_coverage_pct": coverage_pct,
            #         "final_status": status
            #     }, indent=2, default=str)
            # )

            enhanced_list.append({
                "work_order": wo.name,
                "material_status": status,
                "coverage_pct": coverage_pct,
                "remaining_required": remaining_required,
            })


            planned = float(wo.qty or 0)
            produced = float(wo.produced_qty or 0)
            balance_quantity = max(0, planned - produced)

            # Append child row to Bulk Production Page
            parent.append("work_order_list", {
                "work_order_id": wo.name,
                "status": wo.status,
                "sales_order": wo.custom_sales_order,
                "production_plan" : wo.production_plan,
                "balance_quantity": balance_quantity,
                "item_name": wo.production_item,
                "planned_quantity": wo.qty,
                "quantity_produced": wo.produced_qty,
                "inventory_check": status,
                "operations_included": frappe.db.count("Work Order Operation", {"parent": wo.name}) > 0
            })

        parent.save(ignore_permissions=True)

        return {
            "status": "success",
            "data": enhanced_list,
            "message": "Work Orders processed and child table updated"
        }

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Bulk Production - get_workorder_list Error")
        return {"status": "error", "message": str(e)}

@frappe.whitelist()
def refresh_material_status(work_orders):
    """
    Recalculate and return latest material availability & transfer status
    for selected Work Orders passed from the UI.
    """

    try:
        if isinstance(work_orders, str):
            work_orders = json.loads(work_orders)

        if not work_orders:
            return {"status": "error", "message": "No Work Orders received"}

        response_rows = []

        for wo_id in work_orders:
            wo = frappe.get_doc("Work Order", wo_id)

            planned = float(wo.qty or 0)
            produced = float(wo.produced_qty or 0)
            remaining_required = max(0, planned - produced)

            overall_coverage = 1.0

            # Check inventory for required_items
            for comp in wo.required_items:
                required_remaining = max(0, float(comp.required_qty or 0) - float(comp.consumed_qty or 0))
                src_wh = comp.source_warehouse

                if not src_wh:
                    src_wh = frappe.db.get_single_value("Bulk Production Settings", "source_warehouse")

                if not src_wh:
                    response_rows.append({
                        "work_order_id": wo_id,
                        "status": "WAREHOUSE REQUIRED"
                    })
                    continue

                available = frappe.db.get_value("Bin", {"item_code": comp.item_code, "warehouse": src_wh}, "actual_qty") or 0

                if required_remaining > 0:
                    overall_coverage = min(overall_coverage, available / required_remaining)

            # -------------------------------
            # MATERIAL TRANSFER CHECK
            # -------------------------------
            if wo.status == "In Process":
                transfer_qty = frappe.db.sql("""
                    SELECT SUM(fg_completed_qty)
                    FROM `tabStock Entry`
                    WHERE work_order = %s
                    AND stock_entry_type = 'Material Transfer for Manufacture'
                    AND docstatus = 1
                """, (wo.name,))[0][0] or 0

                if transfer_qty >= planned:
                    status = "FULLY TRANSFERRED"
                elif 0 < transfer_qty < planned:
                    status = "PARTIALLY TRANSFERRED"
                else:
                    status = "TRANSFER INITIATED"

            else:
                # Material readiness logic
                if remaining_required == 0:
                    status = "COMPLETED"
                elif overall_coverage >= 1:
                    status = "READY"
                elif overall_coverage >= 0.5:
                    status = "PARTIAL"
                else:
                    status = "INSUFFICIENT"

            response_rows.append({
                "work_order_id": wo_id,
                "status": status,
                "remaining_required": remaining_required
            })

        return {"status": "success", "rows": response_rows}

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Material Refresh Function Error")
        return {"status": "error", "message": str(e)}


# @frappe.whitelist()
# def wo_validations(wo_ids, include_operations=0):
#     # Convert if string
#     if isinstance(wo_ids, str):
#         wo_ids = json.loads(wo_ids)

#     include_operations = frappe.db.get_single_value("Bulk Production Settings", "include_operations")
#     result = []

#     # # Disable all frappe toast messages
#     original_msgprint = frappe.msgprint
#     frappe.msgprint = lambda *args, **kwargs: None

#     try:
#         # fetch once
#         default_src_wh = frappe.db.get_single_value(
#             "Bulk Production Settings",
#             "source_warehouse"
#         )

#         for wo in wo_ids:

#             if not wo or not str(wo).strip():
#                 continue

#             doc = frappe.get_doc("Work Order", wo)
#             changes_made = False

#             # 0️.  INCLUDE LOGIC
#             if include_operations == 0:
#                 # REMOVE all operations before submit
#                 if doc.operations:
#                     doc.operations = []
#                     changes_made = True

#             # 1️.  OPERATION DEFAULTS (ONLY IF include_operations == 1)
#             if include_operations == 1:
#                 for op in doc.operations or []:

#                     # Default time
#                     if not op.time_in_mins or op.time_in_mins == 0:
#                         default_time = frappe.db.get_value(
#                             "Operation", op.operation, "custom_default_time"
#                         )
#                         if default_time:
#                             op.time_in_mins = default_time
#                             changes_made = True

#                     # Default workstation
#                     if not op.workstation:
#                         default_workstation = frappe.db.get_value(
#                             "Operation", op.operation, "workstation"
#                         )
#                         if default_workstation:
#                             op.workstation = default_workstation
#                             changes_made = True

#             # 2️. REQUIRED ITEMS DEFAULT WAREHOUSE
#             if default_src_wh:
#                 for ri in doc.required_items or []:
#                     if not ri.source_warehouse:
#                         ri.source_warehouse = default_src_wh
#                         changes_made = True


#             # 3️. FINISHED GOODS WAREHOUSE CHECK
#             if doc.fg_warehouse:
#                 is_group = frappe.db.get_value("Warehouse", doc.fg_warehouse, "is_group")
            
#             if is_group:

#                 child_wh = frappe.db.get_single_value("Bulk Production Settings", "fg_warehouse") 
#                 if child_wh:
#                     doc.fg_warehouse = child_wh
#                     changes_made = True
#                 else: 
#                     doc.fg_warehouse = "Finished Goods - EIPL"  # Hardcoded fallback
#                     changes_made = True
            
#             #Checking the item code of the work order
#             item_code = doc.production_item
#             item_group_default_wh = None
#             item_group = frappe.db.get_value("Item", item_code, "item_group")
#             if item_group:
#                 item_group_doc = frappe.get_doc("Item Group", item_group)
#                 if item_group_doc:
#                     item_group_defaults = item_group_doc.item_group_defaults
#                     if item_group_defaults:
#                         item_group_default_wh = item_group_defaults[0].default_warehouse

#             # 3️. SAVE CHANGES IF ANY
#             if changes_made:
#                 doc.save()

#             # 4️. SUBMIT WORK ORDER
#             if doc.docstatus == 0:
#                 doc.submit()

#             # 5️. APPEND UPDATED VALUES
#             result.append({
#                 "work_order_id": wo,
#                 "status": doc.status,
#                 "qty": doc.qty,
#                 "produced_qty": doc.produced_qty,
#                 # "planned_start_date": doc.planned_start_date,
#                 # "planned_end_date": doc.planned_end_date
#             })

#         return {
#             "status": "success",
#             "updated_rows": result
#         }

#     except Exception:
#         frappe.log_error(frappe.get_traceback(), "wo_validations Error")
#         return {
#             "status": "error",
#             "message": "Error during WO validation"
#         }

#     finally:
#         # Always restore msgprint
#         frappe.msgprint = original_msgprint


@frappe.whitelist()
def wo_validations(wo_ids, include_operations=0):
    if isinstance(wo_ids, str):
        wo_ids = json.loads(wo_ids)

    success_rows = []  
    error_rows = []     
    updated_rows = []   

    original_msgprint = frappe.msgprint
    frappe.msgprint = lambda *args, **kwargs: None

    try:
        for wo in wo_ids:
            if not wo:
                continue

            try:
                doc = frappe.get_doc("Work Order", wo)
                changes_made = False


                include_operations = frappe.db.get_single_value("Bulk Production Settings", "include_operations")
                if include_operations == 0:
                # REMOVE all operations before submit
                    if doc.operations:
                        doc.operations = []
                        changes_made = True
                
                # 1️.  OPERATION DEFAULTS (ONLY IF include_operations == 1)
                if include_operations == 1:
                    for op in doc.operations or []:

                        # Default time
                        if not op.time_in_mins or op.time_in_mins == 0:
                            default_time = frappe.db.get_value(
                                "Operation", op.operation, "custom_default_time"
                            )
                            if default_time:
                                op.time_in_mins = default_time
                                changes_made = True

                        # Default workstation
                        if not op.workstation:
                            default_workstation = frappe.db.get_value(
                                "Operation", op.operation, "workstation"
                            )
                            if default_workstation:
                                op.workstation = default_workstation
                                changes_made = True


                # ------------------- STEP 1️⃣: FG WAREHOUSE FROM ITEM GROUP -------------------
                production_item = doc.production_item
                if production_item:
                    item_group = frappe.db.get_value("Item", production_item, "item_group")
                    if item_group:
                        ig_doc = frappe.get_doc("Item Group", item_group)
                        
                        # ❗ No defaults rows at all
                        if not ig_doc.item_group_defaults or len(ig_doc.item_group_defaults) == 0:
                            error_rows.append({
                                "work_order_id": wo,
                                "error": f"Item Group '{item_group}' has no Default Warehouse set for Production Item '{production_item}'"
                            })
                            continue  # 🔥 Skip this WO completely

                        # Row exists → extract default warehouse
                        default_fg_wh = ig_doc.item_group_defaults[0].default_warehouse

                        # ❗ First row exists but warehouse value missing/null/empty
                        if not default_fg_wh:
                            error_rows.append({
                                "work_order_id": wo,
                                "error": f"Item Group '{item_group}' Default Warehouse missing for Production Item '{production_item}'"
                            })
                            continue  # 🔥 Skip this WO completely

                        # ✔ Assign if all checks passed
                        if doc.fg_warehouse != default_fg_wh:
                            doc.fg_warehouse = default_fg_wh
                            changes_made = True

                # ------------------- STEP 2️⃣: REQUIRED ITEMS DEFAULT WAREHOUSE -------------------
                for ri in doc.required_items or []:
                    # if not ri.source_warehouse:
                    ri_item_group = frappe.db.get_value("Item", ri.item_code, "item_group")
                    if ri_item_group:
                        rig_doc = frappe.get_doc("Item Group", ri_item_group)
                        
                        # ❗ if NO defaults row → fail this Work Order immediately
                        if not rig_doc.item_group_defaults or len(rig_doc.item_group_defaults) == 0:
                            error_rows.append({
                                "work_order_id": wo,
                                "error": f"Item Group '{ri_item_group}' has no Default Warehouse set for item '{ri.item_code}'"
                            })
                            # Stop processing this WO right here
                            continue

                        # ✔ default exists → assign warehouse
                        ri_default_wh = rig_doc.item_group_defaults[0].default_warehouse
                        if not ri_default_wh:
                            error_rows.append({
                                "work_order_id": wo,
                                "error": f"Default Warehouse missing in Item Group '{ri_item_group}' for item '{ri.item_code}'"
                            })
                            break

                        # ✔ assign default if all OK
                        if ri.source_warehouse != ri_default_wh:
                            ri.source_warehouse = ri_default_wh
                            changes_made = True 
                
                # ------------------- FINAL VALIDATION BEFORE SUBMIT -------------------
                validation_error = None

                # Check FG Warehouse
                if not doc.fg_warehouse:
                    validation_error = "Default Warehouse not found in item group defaults."

                else:
                    # FG warehouse must not be a group warehouse
                    is_group = frappe.db.get_value("Warehouse", doc.fg_warehouse, "is_group")
                    if is_group:
                        # doc.fg_warehouse = frappe.db.get_single_value("Bulk Production Settings","source_warehouse") #For now keeping a fallback but should not happen
                        validation_error = f" '{doc.fg_warehouse}' is a group warehouse."

                # Check required_items warehouse
                for ri in doc.required_items or []:
                    if not ri.source_warehouse:
                        validation_error = f"Source Warehouse missing: {ri.item_code}"
                        break

                    item_wh_group = frappe.db.get_value("Warehouse", ri.source_warehouse, "is_group")
                    if item_wh_group:
                        validation_error = (
                            f"For item {ri.item_code},'{ri.source_warehouse}' is a group warehouse "
                        )
                        break

                if validation_error:
                    error_rows.append({
                        "work_order_id": wo,
                        "error": validation_error
                    })
                    continue  # <--- IMPORTANT: skip further processing for this WO


                # ------------------- SAVE IF MODIFIED -------------------
                if changes_made:
                    doc.save()

                # ------------------- SUBMIT -------------------
                if doc.docstatus == 0:
                    try:
                        doc.submit()
                        success_rows.append({
                            "work_order_id": wo,
                            "status": "Submitted"
                        })
                    except Exception as e:
                        error_rows.append({
                            "work_order_id": wo,
                            "error": str(e)
                        })
                        continue  # don't include in updated_rows refresh

                # ------------------- ALWAYS APPEND TO UPDATED DATA -------------------
                updated_rows.append({
                    "work_order_id": wo,
                    "status": doc.status,
                    "qty": doc.qty,
                    "produced_qty": doc.produced_qty,
                    "fg_warehouse": doc.fg_warehouse
                })

            except Exception as e:
                error_rows.append({
                    "work_order_id": wo,
                    "error": str(e)
                })
                frappe.log_error(frappe.get_traceback(), f"WO Processing Failed: {wo}")

        # --------------- FINAL RESPONSE FOR UI ----------------
        return {
            "status": "success",
            "success_rows": success_rows,
            "error_rows": error_rows,
            "updated_rows": updated_rows
        }

    finally:
        frappe.msgprint = original_msgprint


@frappe.whitelist()
def validate_start_batch(rows=None):

    if isinstance(rows, str):
        rows = json.loads(rows)

    result = []
    processable_count = 0   # count READY rows

    for r in rows:
        wo_id = r.get("work_order_id")
        inventory_check = r.get("inventory_check")

        if not wo_id:
            result.append({
                "work_order_id": None,
                "status": "Cannot Proceed",
                "can_process": False,
                "reason": "Work Order Missing"
            })
            continue

        wo = frappe.get_doc("Work Order", wo_id)

        can_process = True
        reason = ""

        # 🔥 Material requirement validation
        if inventory_check != "READY":
            can_process = False
            reason = f"Material not ready ({inventory_check})"

        # 🔥 Work order status validation
        if wo.status not in ["Not Started", "Draft"]:
            can_process = False
            reason = f"Work Order already started ({wo.status})"

        if can_process:
            processable_count += 1

        result.append({
            "work_order_id": wo_id,
            "inventory_check": inventory_check,
            "planned": wo.qty,
            "produced": wo.produced_qty,
            "status": "Ready" if can_process else "Cannot Proceed",
            "can_process": can_process,
            "reason": reason
        })

    return {
        "status": "can_proceed" if processable_count > 0 else "Cannot Proceed",
        "processable_count": processable_count,
        "rows": result
    }

@frappe.whitelist()
def start_work_orders(wo_ids):

    if isinstance(wo_ids, str):
        wo_ids = json.loads(wo_ids)

    updated_rows = []
    failed_rows = []

    try:
        for wo_id in wo_ids:
            if not wo_id:
                continue

            try:
                # 1️⃣ CHECK IF SE ALREADY EXISTS
                existing_se = frappe.db.exists(
                    "Stock Entry",
                    {
                        "work_order": wo_id,
                        "purpose": "Material Transfer for Manufacture",
                        "docstatus": 1
                    }
                )
                if existing_se:
                    failed_rows.append({
                        "work_order_id": wo_id,
                        "error": f"Stock Entry already exists ({existing_se})"
                    })
                    continue

                # 2️⃣ VALIDATE MATERIAL FROM REQUIRED WAREHOUSE
                wo_doc = frappe.get_doc("Work Order", wo_id)
                insufficient_items = []

                for item in wo_doc.required_items:
                    wh = item.source_warehouse
                    if not wh:
                        insufficient_items.append(f"{item.item_code} (Warehouse missing)")
                        continue

                    qty_available = frappe.db.get_value(
                        "Bin",
                        {"item_code": item.item_code, "warehouse": wh},
                        "actual_qty"
                    ) or 0

                    if qty_available < item.required_qty:
                        insufficient_items.append(
                            f"{item.item_code}: Need {item.required_qty}, Have {qty_available}"
                        )

                if insufficient_items:
                    failed_rows.append({
                        "work_order_id": wo_id,
                        "error": "Insufficient Stock → " + ", ".join(insufficient_items)
                    })
                    continue

                # 3️⃣ MAKE STOCK ENTRY
                qty = wo_doc.qty
                se_response = frappe.call(
                    "erpnext.manufacturing.doctype.work_order.work_order.make_stock_entry",
                    work_order_id=wo_id,
                    purpose="Material Transfer for Manufacture",
                    qty=qty
                )
                # frappe.log_error("SE Response for Starting Work Orders", json.dumps(se_response))
                if not se_response:
                    failed_rows.append({
                        "work_order_id": wo_id,
                        "error": "Failed generating Stock Entry"
                    })
                    continue

                if isinstance(se_response, dict) and "message" in se_response:
                    se_response = se_response["message"]

                if isinstance(se_response, dict):
                    se_doc = frappe.get_doc(se_response)
                else:
                    se_doc = se_response

                se_doc.flags.ignore_permissions = True
                se_doc.insert()
                se_doc.submit()

                # frappe.log_error( "SE Submitted", f"Stock Entry {se_doc.name} submitted for WO {wo_doc.status}")

                updated_rows.append({
                    "work_order_id": wo_id,
                    "status": "In Process",
                    "produced_qty": wo_doc.produced_qty or 0
                })

            except Exception as err:
                failed_rows.append({
                    "work_order_id": wo_id,
                    "error": str(err)
                })
                continue

        frappe.db.commit()

        return {
            "status": "partial" if failed_rows else "success",
            "updated_rows": updated_rows,
            "failed_rows": failed_rows,
            "message": f"{len(updated_rows)} done, {len(failed_rows)} failed"
        }

    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "start_work_orders ERROR")
        return {"status": "error", "message": str(e)}

@frappe.whitelist()
def close_batch(docname=None, rows=None, company=None):
   
    if isinstance(rows, str):
        rows = json.loads(rows)

    try:
        updated_rows = []
        se_list = []

        # # Validate
        # if not rows or not isinstance(rows, list):
        #     return {"status": "error", "message": "Invalid batch row data"}

        if not isinstance(rows, list):
            return {"status": "error", "message": "Invalid batch row data"}

        # If nothing to process, commit & exit gracefully
        if len(rows) == 0:
            return {
                "status": "success",
                "message": "No rows to update. Batch submitted with no changes.",
                "updated_rows": [],
                "se_list": []
            }

        for r in rows:
            wo_id = r.get("work_order_id")
            manufactured_today = float(r.get("manufactured_today") or 0)
            balance_quantity = float(r.get("balance_quantity") or 0)

            if not wo_id:
                return {"status": "error", "message": "Missing Work Order ID in row"}

            if manufactured_today <= 0:
                return {"status": "error", "message": f"Enter Manufactured Qty for Work Order {wo_id}"}

            wo_doc = frappe.get_doc("Work Order", wo_id)

            planned = float(wo_doc.qty or 0)
            produced = float(wo_doc.produced_qty or 0)
            closing_balance = max(0, balance_quantity - manufactured_today)
            closing_status = "Completed" if closing_balance==0 else "In Process"
            # balance_quantity = max(0, planned - produced - manufactured_today)
            # frappe.log_error("CLOSING BATCH - WO CALCULATION", f"WO: {wo_id}, Planned: {planned}, Produced: {produced}, Manufactured Today: {manufactured_today}, Balance: {balance_quantity}, Closing Balance: {closing_balance}, Closing Status: {closing_status}")
            # Try creating SE
            try:
                se_response = make_stock_entry(
                    work_order_id=wo_id,
                    purpose="Manufacture",
                    qty=manufactured_today
                )

                if isinstance(se_response, dict) and "message" in se_response:
                    se_response = se_response["message"]

                if isinstance(se_response, dict):
                    se_doc = frappe.get_doc(se_response)
                else:
                    se_doc = se_response

                default_fg_wh = frappe.db.get_single_value("Bulk Production Settings", "fg_warehouse")
                default_wip_wh = frappe.db.get_single_value("Bulk Production Settings", "wip_warehouse")

                # Header-level
                if se_doc.from_warehouse and frappe.db.get_value("Warehouse", se_doc.from_warehouse, "is_group"):
                    se_doc.from_warehouse = default_wip_wh or default_fg_wh

                if se_doc.to_warehouse and frappe.db.get_value("Warehouse", se_doc.to_warehouse, "is_group"):
                    se_doc.to_warehouse = default_fg_wh

                # Item-level
                for item in se_doc.items:
                    if item.s_warehouse and frappe.db.get_value("Warehouse", item.s_warehouse, "is_group"):
                        item.s_warehouse = default_wip_wh or default_fg_wh

                    if item.t_warehouse and frappe.db.get_value("Warehouse", item.t_warehouse, "is_group"):
                        item.t_warehouse = default_fg_wh

                    # 🔧 If finished item missing target warehouse, assign FG
                    if item.is_finished_item and not item.t_warehouse:
                        item.t_warehouse = default_fg_wh

                se_doc.flags.ignore_permissions = True
                se_doc.insert()
                se_doc.submit()

                se_list.append(se_doc.name)
                # frappe.log_error("SE SUBMIT SUCCESS", f"{se_doc.name} for {wo_id}")

            except Exception as se_error:
                frappe.db.rollback()
                frappe.log_error(frappe.get_traceback(), "SE_CREATION_FAILED")
                return {
                    "status": "error",
                    "message": f"Failed to process WO {wo_id}: {str(se_error)}"
                }

            # Add to return payload but DO NOT update ERP Work Order or Parent here
            updated_rows.append({
                "work_order_id": wo_id,
                "manufactured_today": manufactured_today,
                "balance_quantity": balance_quantity,
                "quantity_produced": produced + manufactured_today,
                "closing_balance": closing_balance,
                "closing_status": closing_status,
                "status": "Completed" if (produced + manufactured_today) >= planned else "In Process"
            })

        frappe.db.commit()

        return {
            "status": "success",
            "message": "Batch closed successfully",
            "updated_rows": updated_rows,
            "se_list": se_list
        }

    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "CLOSE_BATCH_ERROR")
        return {"status": "error", "message": str(e)}

@frappe.whitelist()
def preview_close_batch(rows=None, company=None):

    if isinstance(rows, str):
        rows = json.loads(rows)

    preview_rows = []

    for r in rows:
        wo_id = r.get("work_order_id")
        manufactured_today = float(r.get("manufactured_today") or 0)
        balance_quantity = float(r.get("balance_quantity") or 0)
        status = r.get("status")

        # default decision
        can_process = True
        reason = ""

        # Rule 1: Invalid Status -> Skip immediately, do NOT fetch Work Order
        if status != "In Process":
            can_process = False
            reason = f"Invalid Status ({status})"

            preview_rows.append({
                "work_order_id": wo_id,
                "manufactured_today": manufactured_today,
                "balance_quantity": balance_quantity,
                "closing_balance": balance_quantity,
                "closing_status": status,
                "status": "Cannot Proceed",
                "can_process": False,
                "reason": reason
            })
            continue   # ❌ do not continue further logic

        # Rule 2: Manufactured qty must be > 0 -> Skip immediately, no Work Order call
        if manufactured_today <= 0:
            can_process = False
            reason = "No Quantity Entered"

            preview_rows.append({
                "work_order_id": wo_id,
                "manufactured_today": manufactured_today,
                "balance_quantity": balance_quantity,
                "closing_balance": balance_quantity,
                "closing_status": status,
                "status": "Cannot Proceed",
                "can_process": False,
                "reason": reason
            })
            continue   # ❌ stop here

        # If passed both rules, now fetch Work Order
        wo_doc = frappe.get_doc("Work Order", wo_id)
        planned = float(wo_doc.qty or 0)
        produced = float(wo_doc.produced_qty or 0)

        closing_balance = max(0, balance_quantity - manufactured_today)
        closing_status = "Completed" if closing_balance == 0 else "In Process"

        preview_rows.append({
            "work_order_id": wo_id,
            "manufactured_today": manufactured_today,
            "balance_quantity": balance_quantity,
            "closing_balance": closing_balance,
            "closing_status": closing_status,
            "status": "Will Proceed",
            "can_process": True,
            "reason": "-"
        })

    return {
        "status": "success",
        "preview_rows": preview_rows
    }

@frappe.whitelist()
def fetch_latest_work_orders(company=None, warehouses=None, filters=None):
    """
    Fetch latest Work Orders, compute material indicators,
    and return rows in the same shape as Bulk Production child table.
    """

    try:
        filters = filters or {}
        filters.setdefault("status", ["not in", ["Cancelled", "Closed", "Completed"]])

        if company:
            filters["company"] = company


        # Step 1: Fetch Work Orders
        wo_names = frappe.get_all(
            "Work Order",
            filters=filters,
            pluck="name",
            order_by="creation desc",
            limit_page_length=0
        )

        if not wo_names:
            return {"status": "success", "data": []}

        work_orders = [frappe.get_doc("Work Order", name) for name in wo_names]

        enriched_rows = []

        # Step 2: Process each Work Order
        for wo in work_orders:
            planned = float(wo.qty or 0)
            produced = float(wo.produced_qty or 0)
            balance_quantity = max(0, planned - produced)

            remaining_required = balance_quantity
            overall_coverage = 1.0

            # ---------- MATERIAL COVERAGE ----------
            for comp in wo.required_items:
                required_remaining = max(
                    0,
                    float(comp.required_qty or 0) - float(comp.consumed_qty or 0)
                )

                if required_remaining == 0:
                    continue

                src_wh = comp.source_warehouse or frappe.db.get_single_value(
                    "Bulk Production Settings", "source_warehouse"
                )

                if not src_wh:
                    overall_coverage = 0
                    break

                available = frappe.db.get_value(
                    "Bin",
                    {"item_code": comp.item_code, "warehouse": src_wh},
                    "actual_qty"
                ) or 0

                overall_coverage = min(overall_coverage, available / required_remaining)

            # ---------- MATERIAL / TRANSFER STATUS ----------
            if wo.status == "In Process":
                transfer_qty = frappe.db.sql("""
                    SELECT SUM(se.fg_completed_qty)
                    FROM `tabStock Entry` se
                    WHERE se.work_order = %s
                    AND se.stock_entry_type = 'Material Transfer for Manufacture'
                    AND se.docstatus = 1
                """, (wo.name,))[0][0] or 0

                if transfer_qty >= planned:
                    inventory_status = "FULLY TRANSFERRED"
                elif transfer_qty > 0:
                    inventory_status = "PARTIALLY TRANSFERRED"
                else:
                    inventory_status = "TRANSFER INITIATED"
            else:
                if remaining_required == 0:
                    inventory_status = "COMPLETED"
                elif overall_coverage >= 1:
                    inventory_status = "READY"
                elif overall_coverage >= 0.5:
                    inventory_status = "PARTIAL"
                else:
                    inventory_status = "INSUFFICIENT"

            # ---------- FINAL ROW ----------
            enriched_rows.append({
                "work_order_id": wo.name,
                "status": wo.status,
                "sales_order": wo.custom_sales_order,
                "production_plan": wo.production_plan,
                "balance_quantity": balance_quantity,
                "item_name": wo.production_item,
                "planned_quantity": planned,
                "quantity_produced": produced,
                "inventory_check": inventory_status,
                "operations_included": frappe.db.count(
                    "Work Order Operation", {"parent": wo.name}
                ) > 0
            })

        return {
            "status": "success",
            "data": enriched_rows
        }

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "sync_latest_work_orders Error")
        return {"status": "error", "message": str(e)}

@frappe.whitelist()
def ui_error_log(title, payload=None, level="INFO"):
    """
    Logs UI / User actions to Frappe Error Log
    """

    try:
        message = {
            "user": frappe.session.user,
            "route": frappe.form_dict.get("route"),
            "payload": payload
        }

        frappe.log_error(
            message=json.dumps(message, indent=2, default=str),
            title=f"[UI-{level}] {title}"
        )

        return {"status": "logged"}

    except Exception as e:
        # fallback logging
        frappe.log_error(frappe.get_traceback(), "UI_LOGGER_FAILED")
        return {"status": "failed"}
