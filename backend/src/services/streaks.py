"""产地连续异常预警的纯判定引擎（不依赖数据库，可单测）。

规则口径（与采购约定）：
- 同一产地按入库先后排序，连续三批初检判异常 => 一段，开一张预警单。
- 遇到未判批次先等：未判批次截断当前可确认的连续段，既不计入也不永久打断，
  等判定到达后按原入库位置整列重算。
- 每段只开一单：段与已有预警单按批次集合交集匹配；段延伸沿用原单，
  重算 / 重试不重复开单。
- OPEN（待确认）单的依据随重算实时更新；连续段不再成立时撤回。
- CONFIRMED（已确认）单冻结当时的批次与判定；之后被推翻或段发生变化只挂更正。
"""

ABNORMAL = 'ABNORMAL'
NORMAL = 'NORMAL'
OPEN = 'OPEN'
CONFIRMED = 'CONFIRMED'
WITHDRAWN = 'WITHDRAWN'
MIN_RUN = 3

CONCLUSION_LABEL = {ABNORMAL: '异常', NORMAL: '正常', None: '未判'}


def label(conclusion):
    return CONCLUSION_LABEL.get(conclusion, '未判')


def runs_of(seq):
    """按入库先后遍历批次序列，返回连续异常段（每段为 seq 元素列表）。

    seq: [{'lot_id', 'lot_code', 'conclusion'}]，conclusion ∈ {ABNORMAL, NORMAL, None}。
    None（未判）与 NORMAL 都会截断当前可确认的连续段。
    """
    runs, cur = [], []
    for s in seq:
        if s['conclusion'] == ABNORMAL:
            cur.append(s)
        else:
            if cur:
                runs.append(cur)
                cur = []
    if cur:
        runs.append(cur)
    return runs


def evaluate(seq, alerts):
    """对比当前序列与已存在的预警单，返回需要落库的动作列表。

    seq:    [{'lot_id','lot_code','conclusion'}] 按入库先后排序（conclusion 为当前有效结论）。
    alerts: [{'id','status','basis':[{'lot_id','lot_code','conclusion'}]}] 本产地未终结的预警单。

    返回动作（按序执行即可）：
      {'type':'create',      'lots':[lot_id,...]}        新段开单
      {'type':'update_open', 'alert_id','lots':[...]}    OPEN 单依据刷新（段延伸/收缩）
      {'type':'withdraw',    'alert_id','reason'}        OPEN 单依据不成立/被合并，撤回
      {'type':'correct',     'alert_id','detail'}        CONFIRMED 单挂更正（快照不变）
    """
    concl = {s['lot_id']: s['conclusion'] for s in seq}
    code = {s['lot_id']: s.get('lot_code') or str(s['lot_id']) for s in seq}
    runs = [[s['lot_id'] for s in r] for r in runs_of(seq) if len(r) >= MIN_RUN]
    ordered = sorted(alerts, key=lambda a: a['id'])
    actions, matched, drift = [], set(), {}

    for run in runs:
        rset = set(run)
        hits = [a for a in ordered
                if a['id'] not in matched and {b['lot_id'] for b in a['basis']} & rset]
        if not hits:
            actions.append({'type': 'create', 'lots': run})
            continue
        keeper = hits[0]
        matched.add(keeper['id'])
        if keeper['status'] == OPEN:
            if [b['lot_id'] for b in keeper['basis']] != run:
                actions.append({'type': 'update_open', 'alert_id': keeper['id'], 'lots': run})
        else:  # CONFIRMED：依据冻结，段的延伸/收缩只挂更正
            snap = [b['lot_id'] for b in keeper['basis']]
            if run != snap:
                new = [c for c in run if c not in snap]
                gone = [c for c in snap if c not in run]
                parts = []
                if new:
                    parts.append('段延伸：' + '、'.join(code[c] for c in new) + ' 并入连续异常段')
                if gone:
                    parts.append('、'.join(code[c] for c in gone) + ' 已不在当前连续段内')
                drift.setdefault(keeper['id'], []).extend(parts)
        for other in hits[1:]:  # 同一段撞上了多单：只保留最早一单
            matched.add(other['id'])
            if other['status'] == OPEN:
                actions.append({'type': 'withdraw', 'alert_id': other['id'],
                                'reason': f"与预警 #{keeper['id']} 为同一连续段，每段只开一单，本单并入"})
            else:
                drift.setdefault(other['id'], []).append(
                    f"重算后与预警 #{keeper['id']} 为同一连续段，不再重复开单")

    for a in ordered:  # 没有任何合格段命中的 OPEN 单：依据已不成立
        if a['status'] == OPEN and a['id'] not in matched:
            actions.append({'type': 'withdraw', 'alert_id': a['id'],
                            'reason': '初检结论变化，连续三批异常的依据不再成立'})

    for a in ordered:  # CONFIRMED 单：快照内批次的结论被推翻 => 更正
        if a['status'] != CONFIRMED:
            continue
        for b in a['basis']:
            now = concl.get(b['lot_id'])
            if now != b['conclusion']:
                drift.setdefault(a['id'], []).append(
                    f"批次 {b['lot_code']} 初检结论由 {label(b['conclusion'])} 推翻为 {label(now)}")

    for aid, parts in drift.items():
        actions.append({'type': 'correct', 'alert_id': aid, 'detail': '；'.join(parts)})
    return actions
