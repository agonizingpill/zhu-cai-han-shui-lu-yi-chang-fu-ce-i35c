"""产地连续异常预警的落库层：判定登记、整产地重算、总览查询。

事务约定：所有函数都在调用方的事务里运行（传入同一连接），
登记判定与重算要么一起提交、要么一起回滚，保证重试不重复开单。
"""
import hashlib
import json

from psycopg2.extras import Json, RealDictCursor

from .streaks import CONFIRMED, MIN_RUN, OPEN, evaluate, runs_of

RECOVERED = '后续初检结论使当前依据恢复与确认时一致'


def _hash(basis):
    return hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _load_seq(cur, origin):
    """同一产地按入库先后的批次序列，带每批当前有效的初检结论（最后送检为准）。"""
    cur.execute(
        """SELECT l.id AS lot_id, l.lot_code, l.received_at, i.id AS inspection_id, i.conclusion
           FROM bamboo_lots l
           LEFT JOIN LATERAL (
               SELECT id, conclusion FROM initial_inspections
               WHERE lot_id = l.id ORDER BY submitted_at DESC, id DESC LIMIT 1
           ) i ON true
           WHERE l.origin = %s
           ORDER BY l.received_at, l.id""", (origin,))
    return [dict(r) for r in cur.fetchall()]


def _basis_of(seq, lot_ids):
    by = {s['lot_id']: s for s in seq}
    return [{'lot_id': i,
             'lot_code': by[i]['lot_code'],
             'conclusion': by[i]['conclusion'],
             'inspection_id': by[i]['inspection_id'],
             'received_at': by[i]['received_at'].isoformat() if by[i]['received_at'] else None}
            for i in lot_ids]


def _add_correction(cur, alert_id, detail, seq):
    """挂一条更正；与上一条内容完全相同的漂移不重复记录。"""
    cur.execute('SELECT detail FROM origin_alert_corrections WHERE alert_id=%s ORDER BY id DESC LIMIT 1', (alert_id,))
    last = cur.fetchone()
    if last and last['detail'] == detail:
        return
    snap = [{'lot_id': s['lot_id'], 'lot_code': s['lot_code'], 'conclusion': s['conclusion']} for s in seq]
    cur.execute('INSERT INTO origin_alert_corrections(alert_id, detail, snapshot) VALUES(%s,%s,%s)',
                (alert_id, detail, Json(snap)))


def recompute_origin(c, origin):
    """按入库先后整列重算该产地的连续段与预警单（晚到判定也走这里）。"""
    with c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('origin-alert:' + origin,))
        seq = _load_seq(cur, origin)
        cur.execute("SELECT id, status, basis FROM origin_alerts WHERE origin=%s AND status IN ('OPEN','CONFIRMED') ORDER BY id",
                    (origin,))
        alerts = [{'id': r['id'], 'status': r['status'], 'basis': r['basis']} for r in cur.fetchall()]
        actions = evaluate(seq, alerts)
        for a in actions:
            if a['type'] == 'create':
                basis = _basis_of(seq, a['lots'])
                cur.execute("INSERT INTO origin_alerts(origin, status, basis, basis_hash) VALUES(%s,'OPEN',%s,%s)",
                            (origin, Json(basis), _hash(basis)))
            elif a['type'] == 'update_open':
                basis = _basis_of(seq, a['lots'])
                cur.execute("UPDATE origin_alerts SET basis=%s, basis_hash=%s, updated_at=now() WHERE id=%s AND status='OPEN'",
                            (Json(basis), _hash(basis), a['alert_id']))
            elif a['type'] == 'withdraw':
                cur.execute("UPDATE origin_alerts SET status='WITHDRAWN', withdraw_reason=%s, updated_at=now() WHERE id=%s AND status='OPEN'",
                            (a['reason'], a['alert_id']))
            elif a['type'] == 'correct':
                _add_correction(cur, a['alert_id'], a['detail'], seq)
        # 已确认单：之前有漂移、本轮恢复与快照一致的，补一条恢复更正
        corrected = {a['alert_id'] for a in actions if a['type'] == 'correct'}
        for al in alerts:
            if al['status'] != CONFIRMED or al['id'] in corrected:
                continue
            cur.execute('SELECT detail FROM origin_alert_corrections WHERE alert_id=%s ORDER BY id DESC LIMIT 1', (al['id'],))
            last = cur.fetchone()
            if last and last['detail'] != RECOVERED:
                _add_correction(cur, al['id'], RECOVERED, seq)
        return actions


def record_inspection(c, lot_id, conclusion, source='MANUAL', job_id=None, notes='', created_by=None):
    """登记一条初检判定（同一批次以最后送检的结论为准），随后整产地重算。

    携带 job_id 的上报按任务幂等：同一分析任务重试不重复记录、不重复开单。
    """
    with c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT id, origin, lot_code FROM bamboo_lots WHERE id=%s', (lot_id,))
        lot = cur.fetchone()
        if not lot:
            raise KeyError(lot_id)
        if job_id is not None:
            cur.execute(
                """INSERT INTO initial_inspections(lot_id, conclusion, source, job_id, notes, created_by)
                   VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (job_id) DO NOTHING
                   RETURNING id, submitted_at""",
                (lot_id, conclusion, source, job_id, notes, created_by))
            ins = cur.fetchone()
            if not ins:
                return None, dict(lot), []
        else:
            cur.execute(
                """INSERT INTO initial_inspections(lot_id, conclusion, source, job_id, notes, created_by)
                   VALUES(%s,%s,%s,%s,%s,%s) RETURNING id, submitted_at""",
                (lot_id, conclusion, source, job_id, notes, created_by))
            ins = cur.fetchone()
    actions = recompute_origin(c, lot['origin'])
    return dict(ins), dict(lot), actions


def confirm_alert(c, alert_id, basis_hash, user_id):
    """主管确认：先重算保证依据最新，再比对主管看到的依据指纹。"""
    with c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT id, origin, status FROM origin_alerts WHERE id=%s FOR UPDATE', (alert_id,))
        a = cur.fetchone()
        if not a:
            return 'missing'
        if a['status'] != OPEN:
            return 'not_open'
        recompute_origin(c, a['origin'])
        cur.execute('SELECT status, basis_hash FROM origin_alerts WHERE id=%s', (alert_id,))
        row = cur.fetchone()
        if row['status'] != OPEN:
            return 'withdrawn'
        if row['basis_hash'] != basis_hash:
            return 'stale'
        cur.execute("UPDATE origin_alerts SET status='CONFIRMED', confirmed_by=%s, confirmed_at=now(), updated_at=now() WHERE id=%s",
                    (user_id, alert_id))
        return 'ok'


def list_alerts(c):
    with c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT a.*, u.display_name AS confirmed_by_name
                       FROM origin_alerts a LEFT JOIN users u ON u.id = a.confirmed_by
                       ORDER BY a.id DESC""")
        alerts = [dict(r) for r in cur.fetchall()]
        cur.execute('SELECT id, alert_id, detail, snapshot, created_at FROM origin_alert_corrections ORDER BY id')
        cmap = {}
        for r in cur.fetchall():
            cmap.setdefault(r['alert_id'], []).append(dict(r))
        for a in alerts:
            a['corrections'] = cmap.get(a['id'], [])
        return alerts


def watchlist(c):
    """盯防清单：最近一个正常批次之后，连续异常 1~2 批（未到开单线）的产地。"""
    with c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT l.origin, l.id AS lot_id, l.lot_code, i.conclusion
                       FROM bamboo_lots l
                       LEFT JOIN LATERAL (
                           SELECT conclusion FROM initial_inspections
                           WHERE lot_id = l.id ORDER BY submitted_at DESC, id DESC LIMIT 1
                       ) i ON true
                       ORDER BY l.origin, l.received_at, l.id""")
        rows = [dict(r) for r in cur.fetchall()]
    by_origin = {}
    for r in rows:
        by_origin.setdefault(r['origin'], []).append(r)
    out = []
    for origin, seq in by_origin.items():
        last_normal = max([i for i, s in enumerate(seq) if s['conclusion'] == 'NORMAL'], default=-1)
        zone = seq[last_normal + 1:]
        runs = runs_of(zone)
        streak = len(runs[-1]) if runs else 0
        pending = [s for s in zone if s['conclusion'] is None]
        if 1 <= streak < MIN_RUN:
            out.append({'origin': origin,
                        'streak': streak,
                        'pending': len(pending),
                        'streak_lots': [s['lot_code'] for s in runs[-1]],
                        'pending_lots': [s['lot_code'] for s in pending]})
    return out
