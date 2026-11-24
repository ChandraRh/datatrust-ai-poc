"""
Lineage map: one graph of datasets and the jobs that move data between them, across every connected source.

Each source has a collector that reads what the system itself knows (nothing is typed in by hand):

    PostgreSQL       catalog: which views read which tables (pg_depend); function code parsed for what it reads / writes
    SQL Server       catalog: sys.sql_expression_dependencies; procedure code parsed for what it writes
    Data Factory     pipeline definitions (Copy source -> target, Script SQL, Execute Pipeline), with the table names
                     a run decides at run time taken from recent runs (the Lookup's output)
    POC pipeline     its PostgreSQL catalog (silver -> gold views) + the pipeline's own configuration (files -> bronze -> silver)

The same table seen by two sources gets the same id (model.dataset_id), so the graphs join by themselves:
an ADF Copy that loads salesdb.bronze.payments meets the Sales DB connection's own bronze.payments.

    model.py      node / edge model, ids, the Graph builder
    sql.py        SQL understanding (sqlglot): tables a statement reads and writes, functions it calls
    collect.py    the collectors, one per kind of source
    store.py      app.lineage_snapshots: one graph per source, rebuilt on demand
    graph.py      merge the sources, health from incidents and checks, the part of the map one source sees
"""
