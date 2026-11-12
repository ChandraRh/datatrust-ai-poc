"""
Data checks: generic, scheduled checks on any connected database (Data estate connections).

    dialects.py   SQL per database type (PostgreSQL, SQL Server; Snowflake can be added as one more dialect)
    engine.py     run one check -> PASS / WARN / FAIL / ERROR with the observed value, evidence and the SQL that ran
    store.py      monitor.* tables in DataTrust's own database: check definitions and their result history
    runner.py     scheduler: runs every due check (thread in the web server, or its own process)
    routes.py     /api/estate/checks
"""
