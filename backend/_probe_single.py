
import os, sys, tempfile, time
from pathlib import Path
_T = tempfile.mkdtemp(prefix="w16-probe3-")
os.environ["DATABASE_URL"] = f"sqlite:///{(Path(_T) / 'p.db').as_posix()}"
os.environ["YMONEY_SECRET_KEY"] = "probe-secret-key-not-real-1234567890"
sys.path.insert(0, str(Path.cwd()))
from sqlalchemy import event
from app.db import engine, session_scope
from app.migrations.runner import run_migrations
from app.models import Workspace
from app.services import cost as cost_mod
with session_scope() as s:
    run_migrations(s)
with session_scope() as s:
    ws = Workspace(name="p", slug="p-ws", niche="x"); s.add(ws); s.flush()
    ws.settings_json = {"safety": {"daily_budget_usd": 10**9, "per_video_budget_usd": 10**9}}
    s.commit(); WS = ws.id
stmts = []
@event.listens_for(engine, "before_cursor_execute")
def _rec(conn, cursor, statement, params, context, executemany):
    stmts.append(statement)
cost_mod.reserve_spend(WS, 0.01, category="warm", provider="p")
stmts.clear()
N = 200
t0 = time.perf_counter()
for i in range(N):
    cost_mod.reserve_spend(WS, 0.01, category="t", provider="p")
el = time.perf_counter() - t0
print(f"RESULT {N} reservations in {el:.3f}s = {el/N*1000:.2f} ms each; {len(stmts)/N:.1f} statements/reservation")
