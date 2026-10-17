#!/bin/sh
set -e

if [ "${RESTART_DB:-false}" = "true" ]; then
    echo "RESTART_DB=true: running alembic downgrade base before upgrade"
    alembic downgrade base
fi

alembic upgrade head
python -m sql.bootstrap

exec "$@"
