"""
order_type_decision_tree.py
---------------------------
Implements the Order Type determination decision tree for
Touchless Order Creation (Envalior).

Decision Tree Logic (as per architecture diagram):
─────────────────────────────────────────────────
  START: Material Mapping & Enrichment Completed
    │
    ▼
  Q1: Does the customer have a consignment agreement?
      -> Check Sheet 3: any KB / ZKB order for this customer?
      │
      ├─ NO  -> Order Type = TA  (Standard Order — immediate exit)
      │
      └─ YES -> Q2: Is 'consignment' mentioned in the email?
                │
                ├─ YES -> Historical check (Sheet 3) for KB or ZKB
                │         -> Order Type = KB or ZKB
                │
                └─ NO  -> Historical check (Sheet 3) for customer + material
                           -> Determine Order Type from most-common historical order type
                           -> Fallback: TA

NOTE on customer_group2 (Sheet 2):
  customer_group2 is NOT used in order type determination.
  It is used later in the SO-split logic:
    customer_group2 == 'Z01'  ->  separate Sales Order per line item
    customer_group2 != 'Z01'  ->  one Sales Order per PO (all line items together)

Usage:
    from order_type_decision_tree import OrderTypeDecisionTree

    tree = OrderTypeDecisionTree(df_sheet2, df_sheet3)
    result = tree.determine(
        customer_id='4020002337',
        material_internal='000000000000000931',
        email_text='Please process this consignment order...',
    )
    # result = {'order_type': 'KB', 'reason': '...', 'confidence': 'high'}
"""

import re
from typing import Optional
import pandas as pd


class OrderTypeDecisionTree:
    """
    Implements the order-type determination decision tree using
    Sheet 2 (Customer Master) and Sheet 3 (Historical Orders).
    """

    # Order types considered "consignment"
    CONSIGNMENT_ORDER_TYPES = {'KB', 'ZKB', 'KBN', 'ZCON'}

    # Standard / fallback order type
    DEFAULT_ORDER_TYPE = 'TA'

    # Keywords that trigger the "consignment mentioned in email" branch
    CONSIGNMENT_KEYWORDS = [
        'consignment', 'konsignation', 'consignatie',
        'kb order', 'zkb order', 'konsi',
    ]

    def __init__(self, df_sheet2: pd.DataFrame, df_sheet3: pd.DataFrame):
        """
        Args:
            df_sheet2: Customer Master Data (Sheet 2 from Azure Blob).
                       Used for SO-split logic (customer_group2) — NOT for order type determination.
            df_sheet3: Historical Order Mapping (Sheet 3 from Azure Blob).
                       Used for all order type decision tree lookups (KB/ZKB history,
                       historical order type per customer + material).
        """
        self.df2 = df_sheet2.copy() if df_sheet2 is not None else pd.DataFrame()
        self.df3 = df_sheet3.copy() if df_sheet3 is not None else pd.DataFrame()

        # Normalize string columns — strip whitespace for reliable matching
        self._normalize()

    def _normalize(self):
        """Ensure key columns are string and stripped."""
        for df in (self.df2, self.df3):
            for col in df.columns:
                if df[col].dtype == object:
                    df[col] = df[col].astype(str).str.strip()

    # ─── Main entry point ────────────────────────────────────────────────────

    def determine(
        self,
        customer_id: str,
        material_internal: str = '',
        email_text: str = '',
        sales_organization: str = '',
    ) -> dict:
        """
        Determine the order type for a given customer + material combination.

        Args:
            customer_id:        SAP customer number (e.g. '4020002337')
            material_internal:  Internal material number (e.g. '000000000000000931')
            email_text:         Full text of the incoming email/PO for keyword scan
            sales_organization: Sales org code (e.g. '2500') — optional, improves matching

        Returns:
            dict with keys:
              order_type   (str)  — e.g. 'TA', 'KB', 'ZKB'
              reason       (str)  — human-readable explanation
              confidence   (str)  — 'high' | 'medium' | 'low'
              branch       (str)  — decision path taken
        """
        customer_id       = str(customer_id).strip()
        material_internal = str(material_internal).strip()
        email_text_lower  = email_text.lower()

        # ── Node 1: Does customer have a consignment agreement? ──────────────
        # Check Sheet 3: if this customer has ANY KB or ZKB orders in history
        # they have a consignment agreement and we proceed to Node 2.
        # If NO consignment agreement -> Order Type = TA immediately.
        has_consignment = self._customer_has_consignment(customer_id)

        if not has_consignment:
            # LEFT branch of diagram: NO -> TA
            return {
                'order_type': self.DEFAULT_ORDER_TYPE,
                'reason': (
                    f"Customer {customer_id} has NO consignment agreement: "
                    f"no KB or ZKB orders found in Sheet 3 historical data. "
                    f"Order Type = TA (Standard Order)."
                ),
                'confidence': 'high',
                'branch': 'no_consignment -> TA',
            }

        # ── Node 2: Customer HAS consignment -> check email keyword ─────────
        # RIGHT branch of diagram: YES -> check 'consignment' in email
        consignment_in_email = self._consignment_in_email(email_text_lower)

        if consignment_in_email:
            # YES branch: historical check for KB or ZKB
            consignment_type = self._lookup_consignment_type(customer_id, material_internal)
            if consignment_type:
                return {
                    'order_type': consignment_type,
                    'reason': (
                        f"Customer {customer_id} has consignment agreement AND "
                        f"'consignment' keyword detected in email. "
                        f"Historical Sheet 3 record shows order type '{consignment_type}' "
                        f"for this customer + material."
                    ),
                    'confidence': 'high',
                    'branch': f'has_consignment -> consignment_in_email_yes -> history -> {consignment_type}',
                }
            else:
                # Has consignment + keyword in email, but no specific KB/ZKB history
                # for this material — default to KB
                return {
                    'order_type': 'KB',
                    'reason': (
                        f"Customer {customer_id} has consignment agreement AND "
                        f"'consignment' keyword detected in email, but no specific "
                        f"KB/ZKB history found for this material. Defaulting to KB."
                    ),
                    'confidence': 'medium',
                    'branch': 'has_consignment -> consignment_in_email_yes -> no_history -> KB (default)',
                }

        # NO branch: historical check for customer + material order type
        historical_type = self._lookup_historical_order_type(
            customer_id, material_internal, sales_organization
        )
        if historical_type:
            return {
                'order_type': historical_type,
                'reason': (
                    f"Customer {customer_id} has consignment agreement but "
                    f"'consignment' keyword NOT in email. "
                    f"Historical Sheet 3 lookup (customer + material) returned: '{historical_type}'."
                ),
                'confidence': 'high',
                'branch': f'has_consignment -> consignment_in_email_no -> history -> {historical_type}',
            }

        # ── Fallback ─────────────────────────────────────────────────────────
        return {
            'order_type': self.DEFAULT_ORDER_TYPE,
            'reason': (
                f"Customer {customer_id} has consignment agreement, "
                f"no 'consignment' keyword in email, and no historical "
                f"order type record found for customer + material '{material_internal}'. "
                f"Falling back to: {self.DEFAULT_ORDER_TYPE}."
            ),
            'confidence': 'low',
            'branch': f'has_consignment -> consignment_in_email_no -> no_history -> {self.DEFAULT_ORDER_TYPE} (fallback)',
        }

    # ─── Decision nodes ──────────────────────────────────────────────────────

    def _customer_has_consignment(self, customer_id: str) -> bool:
        """
        Node 1: Check if the customer has an active consignment agreement.

        Source: Sheet 3 (historical order data) only.
        Logic:  If ANY order for this customer has order_type in
                {KB, ZKB, KBN, ZCON} -> the customer has a consignment setup.

        Diagram branch:
          YES -> proceed to Node 2 (email keyword check)
          NO  -> Order Type = TA immediately

        NOTE: customer_group2 (Sheet 2) is deliberately NOT checked here.
              It is reserved for SO-split logic (one SO per PO vs separate SO per line).
        """
        if self.df3.empty or 'customer_id' not in self.df3.columns:
            return False

        mask = self.df3['customer_id'].str.strip() == customer_id
        cust_hist = self.df3[mask]

        if cust_hist.empty or 'order_type' not in cust_hist.columns:
            return False

        hist_types = set(cust_hist['order_type'].str.upper().unique())
        return bool(hist_types & self.CONSIGNMENT_ORDER_TYPES)

    def _consignment_in_email(self, email_text_lower: str) -> bool:
        """
        Node 2: Check if any consignment keyword appears in the email text.
        """
        if not email_text_lower:
            return False
        for kw in self.CONSIGNMENT_KEYWORDS:
            if kw in email_text_lower:
                return True
        return False

    def _lookup_consignment_type(
        self,
        customer_id: str,
        material_internal: str,
    ) -> Optional[str]:
        """
        Historical check: return 'KB' or 'ZKB' if found in Sheet 3 for this customer.
        """
        if self.df3.empty or 'order_type' not in self.df3.columns:
            return None

        mask = self.df3['customer_id'].str.strip() == customer_id
        if material_internal:
            mat_mask = self.df3.get('material_internal', pd.Series(dtype=str)).str.strip() == material_internal
            combined = self.df3[mask & mat_mask]
            if combined.empty:
                combined = self.df3[mask]  # fall back to customer-only
        else:
            combined = self.df3[mask]

        if combined.empty:
            return None

        types_found = combined['order_type'].str.upper().value_counts()
        for otype in types_found.index:
            if otype in self.CONSIGNMENT_ORDER_TYPES:
                return otype

        return None

    def _lookup_historical_order_type(
        self,
        customer_id: str,
        material_internal: str,
        sales_organization: str = '',
    ) -> Optional[str]:
        """
        Historical check: return the most-common order type for customer + material
        from Sheet 3.
        """
        if self.df3.empty or 'order_type' not in self.df3.columns or 'customer_id' not in self.df3.columns:
            return None

        mask = self.df3['customer_id'].str.strip() == customer_id

        # Try customer + material (most specific)
        if material_internal and 'material_internal' in self.df3.columns:
            mat_mask = self.df3['material_internal'].str.strip() == material_internal
            subset = self.df3[mask & mat_mask]
            if not subset.empty:
                return self._most_common_order_type(subset)

        # Try customer + sales_org
        if sales_organization and 'sales_organization' in self.df3.columns:
            org_mask = self.df3['sales_organization'].str.strip() == sales_organization
            subset = self.df3[mask & org_mask]
            if not subset.empty:
                return self._most_common_order_type(subset)

        # Customer only
        subset = self.df3[mask]
        if not subset.empty:
            return self._most_common_order_type(subset)

        return None

    @staticmethod
    def _most_common_order_type(df: pd.DataFrame) -> Optional[str]:
        """Return the most frequently occurring order_type in the given subset."""
        if 'order_type' not in df.columns or df.empty:
            return None
        counts = df['order_type'].str.upper().value_counts()
        if counts.empty:
            return None
        top = counts.index[0]
        return top if top else None

    # ─── Utility ─────────────────────────────────────────────────────────────

    def get_customer_info(self, customer_id: str) -> dict:
        """Return a summary dict from Sheet 2 for a given customer."""
        if self.df2.empty or 'customer_id' not in self.df2.columns:
            return {}
        mask = self.df2['customer_id'].str.strip() == str(customer_id).strip()
        rows = self.df2[mask]
        if rows.empty:
            return {}
        row = rows.iloc[0]
        return row.to_dict()

    def get_order_history_for_customer(self, customer_id: str) -> pd.DataFrame:
        """Return all Sheet 3 rows for a given customer."""
        if self.df3.empty or 'customer_id' not in self.df3.columns:
            return pd.DataFrame()
        mask = self.df3['customer_id'].str.strip() == str(customer_id).strip()
        return self.df3[mask].copy()


# ─── Factory: build tree from Azure Blob ─────────────────────────────────────

def build_decision_tree_from_azure(force_refresh: bool = False) -> OrderTypeDecisionTree:
    """
    Convenience factory: loads Sheet 2 and Sheet 3 from Azure Blob Storage
    (with local cache) and returns a ready-to-use OrderTypeDecisionTree.

    Args:
        force_refresh: If True, bypass local cache and re-download from Azure.

    Returns:
        OrderTypeDecisionTree instance
    """
    from azure_table_reader import AzureTableReader
    reader = AzureTableReader(force_refresh=force_refresh)
    df2 = reader.get_sheet2()
    df3 = reader.get_sheet3()
    return OrderTypeDecisionTree(df_sheet2=df2, df_sheet3=df3)


# ─── CLI demo ─────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import json
    import argparse

    parser = argparse.ArgumentParser(description='Test the Order Type Decision Tree')
    parser.add_argument('--customer',   default='4020002337', help='Customer ID to test')
    parser.add_argument('--material',   default='',           help='Material internal number')
    parser.add_argument('--email',      default='',           help='Email text to scan for keywords')
    parser.add_argument('--sales-org',  default='2500',       help='Sales organization')
    parser.add_argument('--from-azure', action='store_true',  help='Load tables from Azure Blob')
    args = parser.parse_args()

    print("\n" + "="*65)
    print("  Order Type Decision Tree — Test Run")
    print("="*65)

    if args.from_azure:
        print("\nLoading Sheet 2 + Sheet 3 from Azure Blob Storage...")
        tree = build_decision_tree_from_azure()
    else:
        # Local stub for testing without Azure
        print("\n[WARNING] Using empty DataFrames — run with --from-azure for real data.")
        tree = OrderTypeDecisionTree(pd.DataFrame(), pd.DataFrame())

    result = tree.determine(
        customer_id=args.customer,
        material_internal=args.material,
        email_text=args.email,
        sales_organization=args.sales_org,
    )

    print(f"\nResult:\n{json.dumps(result, indent=2)}\n")
    print("="*65 + "\n")
