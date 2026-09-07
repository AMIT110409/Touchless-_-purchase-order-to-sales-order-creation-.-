"""Debug ship-to resolution for MCAM customer 4020042625, postcode 7602 PK."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from sales_order_mapper import SalesOrderMapper
mapper = SalesOrderMapper.from_azure(force_refresh=False)

customer_id = '4020042625'
ship_to_address = 'A. Fokkerweg 2, entrance Maasdijkweg, 7602 PK ALMELO, The Netherlands'
ship_to_postcode = '7602 PK'

print(f'Testing get_ship_to_info_from_test_mp for customer {customer_id}')
print(f'  ship_to_postcode = "{ship_to_postcode}"')
print(f'  ship_to_address  = "{ship_to_address}"')
print()

# Check what ship-to rows exist for this customer in Test MP
raw = mapper._df_mp_raw
subset = raw[raw['sold_to_id'].astype(str).str.strip() == customer_id]
print(f'Test MP rows for {customer_id}: {len(subset)}')
if not subset.empty:
    cols = ['sold_to_id', 'ship_to_id', 'ship_to_postcode', 'ship_to_city', 'ship_to_name_full']
    avail_cols = [c for c in cols if c in subset.columns]
    unique_ship = subset[avail_cols].drop_duplicates()
    print(unique_ship.to_string(index=False))
print()

# Now call the actual resolution function
result = mapper.get_ship_to_info_from_test_mp(
    customer_id=customer_id,
    ship_to_id='',
    ship_to_address=ship_to_address,
    ship_to_postcode=ship_to_postcode,
)
print(f'Result: {result}')
