def open_loans(rows, today, *, overdue_only=False):
    """Return open loan identifiers in register order."""
    return [
        row["id"]
        for row in rows
        if not row["returned"] and (not overdue_only or row["due"] < today)
    ]
