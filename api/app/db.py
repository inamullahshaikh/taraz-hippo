from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .settings import S

pool = ConnectionPool(S.DATABASE_URL, kwargs={"row_factory": dict_row}, min_size=1, max_size=10, open=False)


@contextmanager
def tx(user_id=None, source="App"):
    """One database step. With a user, the database records who acted (begin_action) on every change it audits."""
    if pool.closed:
        pool.open()
    with pool.connection() as conn, conn.transaction():
        conn.execute("SET LOCAL search_path = bomtree, hippo, public")   # people are in hippo (file 6)
        if user_id:
            conn.execute("SELECT begin_action(%s, %s)", (user_id, source))
        else:
            conn.execute("SELECT set_config('bomtree.source', %s, true)", (source,))
        yield conn
