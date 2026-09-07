"""
Professional HTML Email Templates for Envalior Touchless Order Pipeline.
Includes templates for Exception Routing and Robona RPA Notifications.
"""

def get_exception_template(region, filename, failure_reason, message_id, header_fields=None, status=None, missing_materials=None, csr_name=None):
    """
    Returns a formatted HTML string for the Exception Routing email.
    Sent as a SINGLE email with the PO PDF attached.
    """
    header = header_fields or {}
    validation = header.get("validation", {})
    
    # 1. Extracted values from PO
    extracted_sold_to = header.get("sold_to_address") or header.get("customer_id_or_name") or "Not found/extracted"
    extracted_ship_to = header.get("ship_to_address") or "Not found/extracted"
    
    # 2. Sold-to Status & SAP Mapping
    sold_to_id = header.get("customer_number") or header.get("sold_to_id") or validation.get("matched_sold_to_id")
    sold_to_name = header.get("customer_name_matched") or validation.get("matched_sold_to_name")
    
    if sold_to_id:
        sold_to_status = "✅ Matched"
        sold_to_color = "#2e7d32"
        sold_to_id_display = f"{sold_to_id} ({sold_to_name})" if sold_to_name else sold_to_id
    else:
        suggest_id = validation.get("suggestion_sold_to")
        if suggest_id:
            sold_to_status = "⚠️ Unmapped (SAP lacks mapping)"
            sold_to_color = "#ef6c00"
            sold_to_id_display = f"{suggest_id} (Suggested)"
        else:
            sold_to_status = "❌ Missing / Unmapped"
            sold_to_color = "#c62828"
            sold_to_id_display = "N/A"
            
    # 3. Ship-to Status & SAP Mapping
    ship_to_id = header.get("ship_to_id") or validation.get("matched_ship_to_id")
    ship_to_name = header.get("ship_to_name") or validation.get("matched_ship_to_name")
    
    if ship_to_id:
        ship_to_status = "✅ Matched"
        ship_to_color = "#2e7d32"
        ship_to_display = f"{ship_to_id} ({ship_to_name})" if ship_to_name else ship_to_id
    else:
        suggest_id = validation.get("suggestion_ship_to")
        suggest_name = validation.get("suggestion_ship_to_name")
        if suggest_id:
            ship_to_status = "⚠️ Unmapped (SAP lacks mapping)"
            ship_to_color = "#ef6c00"
            ship_to_display = f"{suggest_id} ({suggest_name}) (Suggested)" if suggest_name else f"{suggest_id} (Suggested)"
        else:
            ship_to_status = "❌ Missing / Unmapped"
            ship_to_color = "#c62828"
            ship_to_display = "N/A"

    # 4. Missing Materials warnings block
    missing_materials_html = ""
    if missing_materials:
        items_list = "".join(f"<li style='margin-bottom: 4px;'><code>{m}</code></li>" for m in missing_materials)
        missing_materials_html = f'''
        <div style="background-color: #fff8e1; border-left: 5px solid #ffb300; padding: 15px; margin-bottom: 20px; border-radius: 0 4px 4px 0;">
            <strong style="color: #b57c00;">⚠️ Missing Material Mappings in SAP:</strong>
            <ul style="margin: 8px 0 0 0; padding-left: 20px; font-size: 13px; color: #555;">
                {items_list}
            </ul>
            <p style="margin: 8px 0 0 0; font-size: 12px; color: #666;">
                These customer material numbers/descriptions were found in the PO but could not be mapped to SAP material numbers.
            </p>
        </div>
        '''

    # 5. Recommendation box logic
    recommendation_title = "Manual Order Entry Required"
    recommendation_desc = "Review the attached PO file and failure reason, then manually create the order in SAP."
    
    if status == "EXTRACTION_FAILED":
        recommendation_title = "Check Purchase Order PDF / Create Manually"
        recommendation_desc = "The PDF could not be read or parsed by the AI. Please verify the PO file. You may need to create the Sales Order manually in SAP or request a high-quality PDF from the customer."
    elif status == "MAPPING_FAILED":
        if missing_materials:
            recommendation_title = "Update Material Master Data in SAP"
            recommendation_desc = "The customer/material mappings listed above are missing in SAP master data. Please add these mappings in SAP and wait for the refresh, or process the order manually in SAP."
        elif not sold_to_id or not ship_to_id:
            has_extracted_sold = bool(header.get("sold_to_address") or header.get("customer_id_or_name"))
            has_extracted_ship = bool(header.get("ship_to_address"))
            
            if not has_extracted_sold or not has_extracted_ship:
                missing_party = "Sold-To" if not has_extracted_sold else ("Ship-To" if not has_extracted_ship else "Sold-To and Ship-To")
                recommendation_title = "Request Corrected PO or Create Manually"
                recommendation_desc = f"The PO is missing clear {missing_party} information. Please request a corrected PO from the customer or create the order manually in SAP."
            else:
                recommendation_title = "Update Customer Master Data in SAP"
                recommendation_desc = "The extracted Sold-To / Ship-To addresses could not be matched to existing SAP master data. Please verify if the mapping for this customer / ship-to exists in SAP, or add it."

    greeting = f"Dear {csr_name}," if csr_name else "Hello Customer Service Team,"

    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #333; line-height: 1.6; }}
            .container {{ max-width: 620px; margin: 20px auto; border: 1px solid #e0e0e0; border-radius: 8px; overflow: hidden; }}
            .header {{ background-color: #b71c1c; color: #ffffff; padding: 20px; text-align: center; }}
            .header h1 {{ margin: 0; font-size: 22px; }}
            .content {{ padding: 30px; background-color: #ffffff; }}
            .alert-box {{ background-color: #fff4f4; border-left: 5px solid #d32f2f; padding: 15px; margin-bottom: 20px; border-radius: 0 4px 4px 0; }}
            .rec-box {{ background-color: #f1f8e9; border-left: 5px solid #558b2f; padding: 15px; margin-top: 20px; border-radius: 0 4px 4px 0; }}
            .details-table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
            .details-table th {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid #eeeeee; color: #666; width: 30%; background-color: #fafafa; }}
            .details-table td {{ padding: 10px 8px; border-bottom: 1px solid #eeeeee; font-weight: 500; }}
            .missing-highlight {{ color: #c62828; font-weight: bold; }}
            .footer {{ background-color: #f9f9f9; padding: 20px; text-align: center; font-size: 12px; color: #888; border-top: 1px solid #eeeeee; }}
            .logo-text {{ font-weight: bold; font-size: 28px; letter-spacing: 1px; color: #ffffff; }}
            ol {{ margin: 10px 0; padding-left: 20px; }}
            ol li {{ margin-bottom: 6px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="logo-text">ENVALIOR</div>
                <h1>⚠️ PO Exception — Manual Action Required</h1>
            </div>
            <div class="content">
                <p style="font-size: 15px; margin-top: 0; margin-bottom: 15px;">{greeting}</p>
                <div class="alert-box">
                    <strong>Action Required:</strong> The AI Order Pipeline could not automatically process the
                    incoming Purchase Order below. The PO file is <strong>attached</strong> to this email.
                </div>

                <table class="details-table">
                    <tr><th>Region</th><td>{region}</td></tr>
                    <tr><th>PO File</th><td><strong>{filename}</strong></td></tr>
                    <tr><th>Why it failed</th><td class="missing-highlight">{failure_reason}</td></tr>
                </table>

                {missing_materials_html}

                <h3 style="margin-top: 25px; border-bottom: 2px solid #b71c1c; padding-bottom: 5px; color: #b71c1c; font-size: 16px;">📋 Partner Mapping Details</h3>
                <table class="details-table" style="margin-top: 5px; margin-bottom: 20px;">
                    <tr style="background-color: #fafafa;">
                        <th style="padding: 8px; width: 25%; font-size: 13px;">Role</th>
                        <th style="padding: 8px; width: 45%; font-size: 13px;">Extracted Address / Name</th>
                        <th style="padding: 8px; width: 30%; font-size: 13px;">Status / SAP Mapping</th>
                    </tr>
                    <tr>
                        <td style="padding: 8px; font-size: 13px;"><strong>Sold-To (Customer)</strong></td>
                        <td style="padding: 8px; font-size: 12px; font-weight: normal; color: #333;">{extracted_sold_to}</td>
                        <td style="padding: 8px; font-size: 13px;">
                            <span style="font-weight: bold; color: {sold_to_color};">{sold_to_status}</span><br>
                            <span style="font-size: 11px; color: #555;">SAP ID: <strong>{sold_to_id_display}</strong></span>
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 8px; font-size: 13px;"><strong>Ship-To (Delivery)</strong></td>
                        <td style="padding: 8px; font-size: 12px; font-weight: normal; color: #333;">{extracted_ship_to}</td>
                        <td style="padding: 8px; font-size: 13px;">
                            <span style="font-weight: bold; color: {ship_to_color};">{ship_to_status}</span><br>
                            <span style="font-size: 11px; color: #555;">SAP ID: <strong>{ship_to_display}</strong></span>
                        </td>
                    </tr>
                </table>

                <div class="rec-box">
                    <strong style="color: #33691e; font-size: 15px;">💡 Recommended Action: {recommendation_title}</strong>
                    <p style="margin: 8px 0 12px 0; font-size: 13px; color: #2e7d32; line-height: 1.5;">
                        {recommendation_desc}
                    </p>
                    <strong style="color: #33691e; font-size: 13px; display: block; margin-top: 10px;">General Steps:</strong>
                    <ol style="margin: 5px 0 0 0; padding-left: 20px; font-size: 13px; color: #2e7d32;">
                        <li>Open the <strong>attached PO file</strong> to review the original purchase order.</li>
                        <li>Follow the recommended action above to update SAP or correct the PO.</li>
                        <li>Create or complete the Sales Order in SAP.</li>
                    </ol>
                </div>

                <p style="margin-top: 20px; color: #555; font-size: 13px;">
                    The original PO email has been moved to <em>Exception POs</em> in Outlook for reference.<br>
                    <i>This is an automated notification from the Envalior AI Order Processing Pipeline.</i>
                </p>
            </div>
            <div class="footer">
                &copy; 2026 Envalior. All rights reserved. | IT Supply Chain Automation
            </div>
        </div>
    </body>
    </html>
    '''


def get_robona_template(so_number, message_id, filename, mailbox):
    """
    Returns a formatted HTML string for the Robona RPA Notification email.

    Subject line format (agreed standard):
        {original_PO_subject} SO# {SO1}; {SO2}; ...

    Robona ignores everything LEFT of "SO#" — it reads only what follows.
    Multiple SAP SO numbers are separated by "; ".
    """
    import json
    # so_number may be a single string like "1234567890" or "1234; 5678" (pre-joined)
    so_list = [s.strip() for s in str(so_number).split(";") if s.strip()]
    payload = {
        "action": "ATTACH_EMAIL_TO_SO",
        "so_numbers": so_list,          # list of individual SO numbers
        "so_part": so_number,           # the full SO# token (as it appears in subject)
        "message_id": message_id,
        "mailbox": mailbox,
        "source_file": filename
    }
    json_payload = json.dumps(payload, indent=2)

    # Build a human-readable SO numbers display
    so_display_rows = "".join(
        f"<tr><td style='padding:4px 8px; font-size:18px; color:#2e7d32; font-weight:bold;'>{s}</td></tr>"
        for s in so_list
    ) or f"<tr><td style='padding:4px 8px;'>{so_number}</td></tr>"

    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #333; line-height: 1.6; }}
            .container {{ max-width: 600px; margin: 20px auto; border: 1px solid #e0e0e0; border-radius: 8px; overflow: hidden; }}
            .header {{ background-color: #2e7d32; color: #ffffff; padding: 20px; text-align: center; }}
            .header h1 {{ margin: 0; font-size: 24px; }}
            .content {{ padding: 30px; background-color: #ffffff; }}
            .info-box {{ background-color: #e8f5e9; border-left: 5px solid #2e7d32; padding: 15px; margin-bottom: 20px; }}
            .subject-box {{ background-color: #f3f3f3; border: 1px solid #ccc; border-radius: 4px; padding: 10px 15px; font-family: 'Courier New', monospace; font-size: 13px; margin: 10px 0 20px 0; word-break: break-all; }}
            .details-table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
            .details-table th {{ text-align: left; padding: 8px; border-bottom: 1px solid #eeeeee; color: #666; width: 35%; }}
            .details-table td {{ padding: 8px; border-bottom: 1px solid #eeeeee; font-weight: 500; }}
            .so-table {{ border-collapse: collapse; margin: 6px 0; }}
            .payload-container {{ background-color: #272822; color: #f8f8f2; padding: 15px; border-radius: 4px; font-family: 'Courier New', Courier, monospace; font-size: 13px; margin-top: 20px; overflow-x: auto; }}
            .footer {{ background-color: #f9f9f9; padding: 20px; text-align: center; font-size: 12px; color: #888; border-top: 1px solid #eeeeee; }}
            .logo-text {{ font-weight: bold; font-size: 28px; letter-spacing: 1px; color: #ffffff; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="logo-text">ENVALIOR</div>
                <h1>Robona Attachment Request</h1>
            </div>
            <div class="content">
                <div class="info-box">
                    <strong>Success:</strong> A Sales Order has been created. Please attach the original source email to the SAP record.<br>
                    <small>The email subject line ends with <code>SO# {so_number}</code> — Robona reads only what follows <em>SO#</em>.</small>
                </div>

                <p style="margin-bottom:4px; color:#555;"><strong>Subject line format (standard):</strong></p>
                <div class="subject-box">
                    [Original PO subject] &nbsp;<strong>SO# {so_number}</strong>
                </div>

                <table class="details-table">
                    <tr>
                        <th>SAP Sales Order(s)</th>
                        <td>
                            <table class="so-table">
                                {so_display_rows}
                            </table>
                        </td>
                    </tr>
                    <tr><th>Source File</th><td>{filename}</td></tr>
                    <tr><th>Message ID</th><td><code style="font-size: 11px;">{message_id}</code></td></tr>
                    <tr><th>Source Mailbox</th><td>{mailbox}</td></tr>
                </table>

                <h3 style="margin-top: 30px; border-bottom: 1px solid #eee; padding-bottom: 5px;">RPA Payload</h3>
                <div class="payload-container">
                    <pre style="margin: 0;">{json_payload}</pre>
                </div>

                <p style="margin-top: 20px;">Robona should retrieve the email from the <b>Archive</b> folder and link it to the SO record(s) above.</p>
            </div>
            <div class="footer">
                &copy; 2026 Envalior. All rights reserved. | IT Supply Chain Automation
            </div>
        </div>
    </body>
    </html>
    '''


def get_stage2_so_blocked_template(region, po_number, so_number, block_code, block_reason):
    """
    Stage 2 exception email: SAP Sales Order created but has an order block.
    CSR needs to release the block in SAP.
    """
    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #333; line-height: 1.6; }}
            .container {{ max-width: 600px; margin: 20px auto; border: 1px solid #e0e0e0; border-radius: 8px; overflow: hidden; }}
            .header {{ background-color: #e65100; color: #ffffff; padding: 20px; text-align: center; }}
            .header h1 {{ margin: 0; font-size: 22px; }}
            .content {{ padding: 30px; background-color: #ffffff; }}
            .alert-box {{ background-color: #fff8e1; border-left: 5px solid #f57c00; padding: 15px; margin-bottom: 20px; }}
            .details-table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
            .details-table th {{ text-align: left; padding: 8px; border-bottom: 1px solid #eeeeee; color: #666; width: 35%; }}
            .details-table td {{ padding: 8px; border-bottom: 1px solid #eeeeee; font-weight: 500; }}
            .so-number {{ font-size: 20px; font-weight: bold; color: #e65100; }}
            .footer {{ background-color: #f9f9f9; padding: 20px; text-align: center; font-size: 12px; color: #888; border-top: 1px solid #eeeeee; }}
            .logo-text {{ font-weight: bold; font-size: 28px; letter-spacing: 1px; color: #ffffff; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="logo-text">ENVALIOR</div>
                <h1>Sales Order Created &mdash; Order Block Detected</h1>
            </div>
            <div class="content">
                <div class="alert-box">
                    <strong>Action Required:</strong> A Sales Order was automatically created from a Purchase Order,
                    but it has been placed on <strong>hold</strong> and requires your attention before it can proceed.
                </div>
                <table class="details-table">
                    <tr><th>Region</th><td>{region}</td></tr>
                    <tr><th>PO Number</th><td>{po_number}</td></tr>
                    <tr><th>SAP Sales Order</th><td><span class="so-number">{so_number}</span></td></tr>
                    <tr><th>Block Code</th><td>{block_code or "N/A"}</td></tr>
                    <tr><th>Block Reason</th><td><strong style="color:#c62828;">{block_reason}</strong></td></tr>
                </table>
                <p style="margin-top: 30px;">
                    Please <b>review and release the order block</b> in SAP for Sales Order <strong>{so_number}</strong>.<br><br>
                    The email has been kept in <em>Processed POs</em> in Outlook as the pipeline completed its steps.
                </p>
                <p><i>This is an automated Stage 2 notification from the Touchless Order Pipeline.</i></p>
            </div>
            <div class="footer">&copy; 2026 Envalior. All rights reserved. | IT Supply Chain Automation</div>
        </div>
    </body>
    </html>
    '''


def get_stage2_so_failed_template(region, po_number, failure_reason,
                                   celonis_run_id="",
                                   rfc_error_code="", rfc_message=""):
    """
    Stage 2 exception email: Celonis Action Flow failed to create the SAP Sales Order.
    CSR must create the SO manually in SAP.

    Args:
        region:          Region label (e.g. 'EMEA', 'APAC')
        po_number:       Customer PO number
        failure_reason:  Combined failure reason (may include RFC error code/message)
        celonis_run_id:  Optional Celonis Action Flow run ID for tracing
        rfc_error_code:  SAP RFC error code (e.g. BAPI class ID from RETURN table)
        rfc_message:     Exact SAP RFC error message from BAPI RETURN table
    """
    run_id_row = (f"<tr><th>Celonis Run ID</th><td><code>{celonis_run_id}</code></td></tr>"
                  if celonis_run_id else "")

    # ── Build the RFC error detail box ───────────────────────────────────────
    # Show a prominent red box with the exact SAP error when RFC details exist.
    rfc_detail_html = ""
    if rfc_error_code or rfc_message:
        code_part = (f"<tr><td style='padding:4px 0; color:#888; width:120px;'>"
                     f"<strong>Error Code:</strong></td>"
                     f"<td style='padding:4px 0;'><code style='background:#f5f5f5; "
                     f"padding:2px 6px; border-radius:3px;'>{rfc_error_code}</code></td></tr>"
                     if rfc_error_code else "")
        msg_part = (f"<tr><td style='padding:4px 0; color:#888;'>"
                    f"<strong>RFC Message:</strong></td>"
                    f"<td style='padding:4px 0; color:#b71c1c; font-weight:500;'>{rfc_message}</td></tr>"
                    if rfc_message else "")
        rfc_detail_html = f"""
        <div style="background-color:#fff3f3; border:2px solid #d32f2f; border-radius:6px;
                    padding:16px; margin:20px 0;">
            <div style="display:flex; align-items:center; margin-bottom:10px;">
                <span style="font-size:18px; margin-right:8px;">&#x274C;</span>
                <strong style="color:#b71c1c; font-size:15px;">
                    SAP RFC Error (BAPI_SALESORDER_CREATEFROMDAT2 RETURN)
                </strong>
            </div>
            <table style="border-collapse:collapse; width:100%; font-size:13px;">
                {code_part}
                {msg_part}
            </table>
        </div>"""
    elif failure_reason:
        # No structured RFC fields but we have a reason string — show it in a box
        rfc_detail_html = f"""
        <div style="background-color:#fff3f3; border:2px solid #d32f2f; border-radius:6px;
                    padding:16px; margin:20px 0;">
            <div style="display:flex; align-items:center; margin-bottom:8px;">
                <span style="font-size:18px; margin-right:8px;">&#x274C;</span>
                <strong style="color:#b71c1c; font-size:15px;">SAP RFC / Action Flow Error</strong>
            </div>
            <p style="margin:0; font-size:13px; color:#b71c1c; font-weight:500;">{failure_reason}</p>
        </div>"""

    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #333; line-height: 1.6; }}
            .container {{ max-width: 620px; margin: 20px auto; border: 1px solid #e0e0e0; border-radius: 8px; overflow: hidden; }}
            .header {{ background-color: #b71c1c; color: #ffffff; padding: 20px; text-align: center; }}
            .header h1 {{ margin: 0; font-size: 22px; }}
            .content {{ padding: 30px; background-color: #ffffff; }}
            .alert-box {{ background-color: #ffebee; border-left: 5px solid #d32f2f; padding: 15px; margin-bottom: 20px; border-radius: 0 4px 4px 0; }}
            .details-table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
            .details-table th {{ text-align: left; padding: 8px; border-bottom: 1px solid #eeeeee; color: #666; width: 35%; }}
            .details-table td {{ padding: 8px; border-bottom: 1px solid #eeeeee; font-weight: 500; }}
            .footer {{ background-color: #f9f9f9; padding: 20px; text-align: center; font-size: 12px; color: #888; border-top: 1px solid #eeeeee; }}
            .logo-text {{ font-weight: bold; font-size: 28px; letter-spacing: 1px; color: #ffffff; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="logo-text">ENVALIOR</div>
                <h1>&#x26A0; SAP Sales Order Creation Failed</h1>
            </div>
            <div class="content">
                <div class="alert-box">
                    <strong>Action Required:</strong> The Celonis Action Flow called the SAP RFC module
                    (<code>BAPI_SALESORDER_CREATEFROMDAT2</code>) to create a Sales Order from the
                    extracted PO data, but the creation <strong>failed</strong>.
                    Please create this order manually in SAP.
                </div>

                {rfc_detail_html}

                <table class="details-table">
                    <tr><th>Region</th><td>{region}</td></tr>
                    <tr><th>PO Number</th><td><strong>{po_number}</strong></td></tr>
                    <tr>
                        <th>Failure Reason</th>
                        <td><span style="color:#c62828;">{failure_reason or "SAP RFC call failed — see Celonis Action Flow logs"}</span></td>
                    </tr>
                    {run_id_row}
                </table>

                <p style="margin-top: 30px;">
                    The original PO email has been moved to <em>Exception POs</em> in Outlook for your reference.<br><br>
                    Please create the Sales Order manually in SAP using the PO details.<br>
                    If this failure repeats, contact the IT Automation team for investigation.
                </p>
                <p><i>This is an automated Stage 2 notification from the Touchless Order Pipeline.</i></p>
            </div>
            <div class="footer">&copy; 2026 Envalior. All rights reserved. | IT Supply Chain Automation</div>
        </div>
    </body>
    </html>
    '''
