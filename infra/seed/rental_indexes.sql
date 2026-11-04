CREATE INDEX vehicles_tenant_idx ON rental.vehicles (tenant_id);
CREATE INDEX customers_tenant_idx ON rental.customers (tenant_id);
CREATE INDEX agreements_tenant_idx ON rental.rental_agreements (tenant_id, start_ts);
CREATE INDEX agreements_vehicle_idx ON rental.rental_agreements (vehicle_id);
CREATE INDEX invoices_tenant_idx ON rental.invoices (tenant_id, issued_on);
CREATE INDEX invoices_agreement_idx ON rental.invoices (agreement_id);
CREATE INDEX invoice_lines_invoice_idx ON rental.invoice_lines (invoice_id);
CREATE INDEX damages_tenant_idx ON rental.vehicle_damages (tenant_id, reported_on);
CREATE INDEX damages_vehicle_idx ON rental.vehicle_damages (vehicle_id);

COMMENT ON TABLE rental.tenants IS 'Rental companies on the platform.';
COMMENT ON TABLE rental.invoices IS 'One rental can have several invoices, and credit notes are negative.';
COMMENT ON COLUMN rental.invoices.total_cents IS 'In the invoice currency, including tax.';
COMMENT ON COLUMN rental.vehicle_damages.estimated_cost_cents IS 'Assessor estimate. final_cost_cents replaces it once repaired.';
