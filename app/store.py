import sqlite3, json
from pathlib import Path
from datetime import datetime, timezone
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("""CREATE TABLE IF NOT EXISTS publications (
        slot INTEGER PRIMARY KEY CHECK (slot=1),
        run_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        revision INTEGER NOT NULL,
        result TEXT NOT NULL,
        published_at TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES runs(id))""")
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def current_publication(path):
    with connect(path) as con:
        row=con.execute('SELECT run_id,digest,revision,published_at FROM publications WHERE slot=1').fetchone()
    return dict(row) if row else None

def publish(path, run_id, revision):
    with connect(path) as con:
        row=con.execute('SELECT id,digest,result,revision FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row:
            raise BusinessError('NOT_FOUND','批次不存在',404)
        if row['revision']!=revision:
            raise BusinessError('STALE_VERSION','批次版本已更新，请刷新后再发布',409)
        result=json.loads(row['result'])
        # 仅允许账实相符、退货审计无异常的批次发布；校验失败不改任何已发布结果。
        if not (result.get('balance',{}).get('row_balanced')
                and result.get('balance',{}).get('points_balanced')):
            raise BusinessError('PUBLISH_UNBALANCED','积分守恒未通过，禁止发布',422)
        if result.get('refund_issues'):
            raise BusinessError('PUBLISH_REFUND_ISSUE','退货回拨存在未平账记录，禁止发布',422)
        existing=con.execute('SELECT run_id,digest,revision FROM publications WHERE slot=1').fetchone()
        if existing and existing['run_id']==run_id and existing['digest']==row['digest'] \
                and existing['revision']==revision:
            # 同一结果重试：幂等返回，不产生第二条发布记录、不重复恢复任何积分。
            published_at=con.execute('SELECT published_at FROM publications WHERE slot=1').fetchone()['published_at']
            return {'run_id':run_id,'digest':row['digest'],'revision':revision,
                    'published_at':published_at,'replayed':False}
        payload=json.dumps({'run_id':run_id,'digest':row['digest'],'revision':revision,
                            'result':result},ensure_ascii=False)
        published_at=datetime.now(timezone.utc).isoformat()
        # UPSERT 为单条原子语句：发布失败（约束/磁盘等）时旧版整行保留。
        con.execute("""INSERT INTO publications(slot,run_id,digest,revision,result,published_at)
                       VALUES(1,?,?,?,?,?)
                       ON CONFLICT(slot) DO UPDATE SET
                         run_id=excluded.run_id,digest=excluded.digest,revision=excluded.revision,
                         result=excluded.result,published_at=excluded.published_at""",
                    (run_id,row['digest'],revision,payload,published_at))
    return {'run_id':run_id,'digest':row['digest'],'revision':revision,
            'published_at':published_at,'replayed':not (existing and existing['run_id']==run_id)}

def withdraw(path, run_id):
    with connect(path) as con:
        existing=con.execute('SELECT run_id FROM publications WHERE slot=1').fetchone()
        if not existing:
            raise BusinessError('NOT_PUBLISHED','当前没有已发布结果',409)
        if existing['run_id']!=run_id:
            raise BusinessError('PUBLICATION_MISMATCH','仅允许撤回当前已发布的同一批次',409)
        con.execute('DELETE FROM publications WHERE slot=1')
    return {'run_id':run_id,'withdrawn':True}
