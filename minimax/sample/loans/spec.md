# LOAN-1 Open loans
Exclude returned loans, retaining the input order and identifiers. Empty input returns an empty list.

# LOAN-2 Overdue loans
In overdue-only mode, include an open loan only when its due date is strictly earlier than the supplied business date. A loan is still on time on its due date. Dates are ISO YYYY-MM-DD.
