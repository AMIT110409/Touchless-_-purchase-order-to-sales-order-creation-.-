import sys
from docx import Document
from docx.shared import Pt

def update_document(file_path, output_path):
    doc = Document(file_path)
    
    # Trackers to know where to insert
    decision_tree_found = False
    decision_tree_idx = -1

    for i, p in enumerate(doc.paragraphs):
        # 1. Update prerequisites
        if "Excel Mapping File" in p.text and "business team" in p.text:
            p.text = "Decision Tree & Transactional Model — Replaces static Excel mapping. Determines Sales Org, Order Type, and splitting rules dynamically based on historical transactional data and logical conditions."
        
        # 2. Update phase 1 options
        if "Option A: Excel Mapping (Current):" in p.text:
            p.text = "Option A: Decision Tree Logic: Replaces manual lookup files. Order type is dynamically determined based on historical transaction data and consignment indicators."
        
        if "Option B: Transactional Data" in p.text:
            p.text = "Option B: Transactional History Check: AI fetches Order Type from past transaction history specifically matching the customer and material."

        if "Decision Tree for order type Confirmation" in p.text:
            decision_tree_idx = i

    # Inject the Decision Tree mapping logic
    if decision_tree_idx != -1:
        # We insert backwards so they appear in order
        steps = [
            "",
            "The Order Type determination follows this hierarchical decision tree right after Material Mapping & Enrichment:",
            "1. Decision Node: Does the customer have a consignment block?",
            "   ➔ Yes: Order Type = TA (Standard Order)",
            "   ➔ No: Proceed to step 2.",
            "2. Decision Node: Is 'consignment' explicitly mentioned in the email?",
            "   ➔ Yes: System performs a historical check for (KB or ZKB).",
            "          - Outcome: Order Type = KB or ZKB",
            "   ➔ No: System performs a full historical check for the specific customer/material combo.",
            "          - Outcome: Determine Order Type from History",
            ""
        ]
        
        # Insert them after the heading
        # Paragraphs in docx don't have a direct insert_after, but insert_paragraph_before on the next paragraph works
        if decision_tree_idx + 1 < len(doc.paragraphs):
            next_p = doc.paragraphs[decision_tree_idx + 1]
            for step in steps:
                next_p.insert_paragraph_before(step)
        else:
            for step in steps:
                doc.add_paragraph(step)

    doc.save(output_path)
    print(f"Document saved to {output_path}")

if __name__ == "__main__":
    update_document('Touchless_Order_Creation_Documentation_UPDATED - Copy (1).docx', 'Touchless_Order_Creation_Documentation_FINAL.docx')
