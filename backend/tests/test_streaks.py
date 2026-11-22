"""产地连续异常预警引擎（streaks.evaluate）的单元测试。

纯标准库，直接运行：python3 backend/tests/test_streaks.py
也兼容 pytest：函数名均为 test_*。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.services.streaks import ABNORMAL as A
from src.services.streaks import NORMAL as N
from src.services.streaks import CONFIRMED, OPEN, evaluate, runs_of

P = None  # 未判


def seq(*cs):
    return [{'lot_id': i + 1, 'lot_code': f'BZ-{i + 1}', 'conclusion': c} for i, c in enumerate(cs)]


def mk_alert(aid, status, lots):
    return {'id': aid, 'status': status,
            'basis': [{'lot_id': s['lot_id'], 'lot_code': s['lot_code'], 'conclusion': s['conclusion']} for s in lots]}


def types(actions):
    return [a['type'] for a in actions]


def test_runs_pending_breaks_but_waits():
    # 未判批次截断可确认连续段：A A 未判 A => [A A] 与 [A]，等中间判定
    runs = runs_of(seq(A, A, P, A))
    assert [[s['lot_id'] for s in r] for r in runs] == [[1, 2], [4]]


def test_three_consecutive_opens_one_ticket():
    actions = evaluate(seq(A, A, A), [])
    assert types(actions) == ['create'] and actions[0]['lots'] == [1, 2, 3]


def test_five_in_a_row_still_one_ticket():
    # 每段只开一单：5 连异常只开一张单
    actions = evaluate(seq(A, A, A, A, A), [])
    assert types(actions) == ['create'] and actions[0]['lots'] == [1, 2, 3, 4, 5]


def test_pending_means_wait_no_alert():
    assert evaluate(seq(A, A, P), []) == []
    assert evaluate(seq(A, A, P, A), []) == []  # 跨过未判批不算连续三批


def test_late_judgment_recomputes_by_inbound_position():
    # 晚到的判定按原入库位置补齐后，整列重算成一段
    first = evaluate(seq(A, A, P, A, A), [])
    assert first == []
    second = evaluate(seq(A, A, A, A, A), [])
    assert types(second) == ['create'] and second[0]['lots'] == [1, 2, 3, 4, 5]


def test_normal_breaks_streak():
    assert evaluate(seq(A, A, N, A, A), []) == []


def test_recompute_is_idempotent_no_duplicate_ticket():
    # 重试/重复重算不重复开单
    alert = mk_alert(1, OPEN, seq(A, A, A))
    assert evaluate(seq(A, A, A), [alert]) == []


def test_open_alert_extends_with_same_ticket():
    alert = mk_alert(1, OPEN, seq(A, A, A))
    actions = evaluate(seq(A, A, A, A), [alert])
    assert types(actions) == ['update_open'] and actions[0]['lots'] == [1, 2, 3, 4]


def test_open_alert_shrinks_but_keeps_ticket():
    alert = mk_alert(1, OPEN, seq(A, A, A, A))
    actions = evaluate(seq(N, A, A, A), [alert])
    assert types(actions) == ['update_open'] and actions[0]['lots'] == [2, 3, 4]


def test_open_alert_withdrawn_when_basis_gone():
    alert = mk_alert(1, OPEN, seq(A, A, A))
    actions = evaluate(seq(A, N, A), [alert])
    assert types(actions) == ['withdraw'] and actions[0]['alert_id'] == 1


def test_new_segment_after_normal_opens_new_ticket():
    alert = mk_alert(1, OPEN, seq(A, A, A))
    actions = evaluate(seq(A, A, A, N, A, A, A), [alert])
    assert types(actions) == ['create'] and actions[0]['lots'] == [5, 6, 7]


def test_confirmed_alert_frozen_and_correction_on_flip():
    s = seq(A, A, A)
    alert = mk_alert(1, CONFIRMED, s)
    actions = evaluate(seq(A, N, A), [alert])
    assert types(actions) == ['correct']
    assert 'BZ-2' in actions[0]['detail'] and '推翻' in actions[0]['detail']


def test_confirmed_alert_extension_only_correction():
    alert = mk_alert(1, CONFIRMED, seq(A, A, A))
    actions = evaluate(seq(A, A, A, A), [alert])
    assert types(actions) == ['correct'] and '段延伸' in actions[0]['detail']


def test_confirmed_alert_no_drift_no_action():
    alert = mk_alert(1, CONFIRMED, seq(A, A, A))
    assert evaluate(seq(A, A, A), [alert]) == []


def test_two_segments_merge_into_oldest_ticket():
    a1 = mk_alert(1, OPEN, seq(A, A, A))
    a2 = mk_alert(2, OPEN, [ {'lot_id': 5, 'lot_code': 'BZ-5', 'conclusion': A},
                             {'lot_id': 6, 'lot_code': 'BZ-6', 'conclusion': A},
                             {'lot_id': 7, 'lot_code': 'BZ-7', 'conclusion': A}])
    # 中间第 4 批晚到判定为异常，两段并成一段：保留最早一单，另一单撤回
    actions = evaluate(seq(A, A, A, A, A, A, A), [a1, a2])
    assert types(actions) == ['update_open', 'withdraw']
    assert actions[0]['alert_id'] == 1 and actions[0]['lots'] == [1, 2, 3, 4, 5, 6, 7]
    assert actions[1]['alert_id'] == 2


def test_confirmed_split_creates_new_segment_ticket():
    # 已确认段中间被推翻：原单挂更正，后半截新段重新开单
    lots = [{'lot_id': i, 'lot_code': f'BZ-{i}', 'conclusion': A} for i in range(1, 8)]
    alert = mk_alert(1, CONFIRMED, lots)
    s = seq(A, A, A, N, A, A, A)
    actions = evaluate(s, [alert])
    assert sorted(types(actions)) == ['correct', 'create']
    create = next(a for a in actions if a['type'] == 'create')
    assert create['lots'] == [5, 6, 7]


def test_run_of_two_plus_pending_shows_waiting_not_alert():
    # 2 连异常 + 后续待判：不开单（先等）
    assert evaluate(seq(A, A, P, P), []) == []


def test_open_alert_splits_into_two_segments():
    # OPEN 单中间批被推翻：原单收缩到前半段，后半段成为新段另开一单
    lots = [{'lot_id': i, 'lot_code': f'BZ-{i}', 'conclusion': A} for i in range(1, 8)]
    alert = mk_alert(1, OPEN, lots)
    actions = evaluate(seq(A, A, A, N, A, A, A), [alert])
    assert sorted(types(actions)) == ['create', 'update_open']
    update = next(a for a in actions if a['type'] == 'update_open')
    create = next(a for a in actions if a['type'] == 'create')
    assert update['alert_id'] == 1 and update['lots'] == [1, 2, 3]
    assert create['lots'] == [5, 6, 7]


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
