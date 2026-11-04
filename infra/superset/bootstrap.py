"""Register Trino in Superset as Nova's single cross-source database.

Runs inside the Superset app context during init, and is idempotent. It sits
here rather than in the agent because Superset has to know about Trino before
any source is connected.
"""

import json
import os

from superset.app import create_app

DATABASE_NAME = "Nova Trino"


def main() -> None:
    app = create_app()
    with app.app_context():
        from superset import db
        from superset.models.core import Database

        uri = os.environ["SUPERSET_TRINO_URI"]
        database = db.session.query(Database).filter_by(database_name=DATABASE_NAME).one_or_none()
        created = database is None
        if created:
            database = Database(database_name=DATABASE_NAME)
            db.session.add(database)

        database.set_sqlalchemy_uri(uri)
        database.expose_in_sqllab = True
        database.allow_run_async = True
        # Nova's Superset connection only ever reads.
        database.allow_ctas = False
        database.allow_cvas = False
        database.allow_dml = False
        database.extra = json.dumps(
            {
                "allow_multi_catalog": True,
                "allows_virtual_table_explore": True,
                "engine_params": {"connect_args": {"source": "nova-superset"}},
            }
        )
        db.session.commit()
        print(f"[nova] {'created' if created else 'updated'} database '{DATABASE_NAME}' (id={database.id})")


if __name__ == "__main__":
    main()
