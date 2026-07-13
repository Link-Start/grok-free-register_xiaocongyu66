"""
Observer — 只读观测层

只记录指标、生成日志。不修改 Semaphore,不调度 Worker,不释放资源。
"""
import time
from collections import deque


class Metrics:
    """全局指标收集器。所有字段只在 asyncio 单线程中写入,无需加锁。"""

    __slots__ = (
        'start_time',
        't_produced', 't_admitted', 't_claimed', 't_expired', 't_discarded',
        't_solve_count', 't_solve_seconds', 't_solve_failed',
        'solver_goto_seconds', 'solver_inject_seconds', 'solver_initial_seconds',
        'solver_click_seconds', 'solver_wait_seconds', 'solver_reused_count',
        'solver_visible_frame_count',
        's_physical_count', 's_physical_wait_seconds', 's_physical_hold_seconds',
        'p_physical_count', 'p_physical_wait_seconds', 'p_physical_hold_seconds',
        'c_physical_count', 'c_physical_wait_seconds', 'c_physical_hold_seconds',
        'p_email_create_count', 'p_email_create_seconds',
        'p_page_prepare_count', 'p_page_prepare_seconds',
        'p_send_count', 'p_send_seconds',
        'c_page_acquire_count', 'c_page_acquire_seconds',
        'c_verify_count', 'c_verify_seconds',
        'c_register_count', 'c_register_seconds',
        'c_hot_page_hits', 'c_hot_page_misses',
        'q_sent', 'q_returned', 'q_admitted', 'q_claimed', 'q_expired', 'q_discarded',
        'q_send_batches', 'q_send_batch_items',
        'pair_claimed', 'pair_consumed_ok', 'pair_consumed_fail',
        'success_count',
        'registration_starts',
        '_clock', 'started_monotonic', 'recent_success_times',
    )

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.started_monotonic = clock()
        self.recent_success_times = deque()
        self.start_time = time.time()
        # T 生命周期
        self.t_produced = 0
        self.t_admitted = 0
        self.t_claimed = 0
        self.t_expired = 0
        self.t_discarded = 0
        self.t_solve_count = 0
        self.t_solve_seconds = 0.0
        self.t_solve_failed = 0
        self.solver_goto_seconds = 0.0
        self.solver_inject_seconds = 0.0
        self.solver_initial_seconds = 0.0
        self.solver_click_seconds = 0.0
        self.solver_wait_seconds = 0.0
        self.solver_reused_count = 0
        self.solver_visible_frame_count = 0
        self.s_physical_count = 0
        self.s_physical_wait_seconds = 0.0
        self.s_physical_hold_seconds = 0.0
        self.p_physical_count = 0
        self.p_physical_wait_seconds = 0.0
        self.p_physical_hold_seconds = 0.0
        self.c_physical_count = 0
        self.c_physical_wait_seconds = 0.0
        self.c_physical_hold_seconds = 0.0
        self.p_email_create_count = 0
        self.p_email_create_seconds = 0.0
        self.p_page_prepare_count = 0
        self.p_page_prepare_seconds = 0.0
        self.p_send_count = 0
        self.p_send_seconds = 0.0
        self.c_page_acquire_count = 0
        self.c_page_acquire_seconds = 0.0
        self.c_verify_count = 0
        self.c_verify_seconds = 0.0
        self.c_register_count = 0
        self.c_register_seconds = 0.0
        self.c_hot_page_hits = 0
        self.c_hot_page_misses = 0
        # Q 生命周期
        self.q_sent = 0
        self.q_returned = 0
        self.q_admitted = 0
        self.q_claimed = 0
        self.q_expired = 0
        self.q_discarded = 0
        self.q_send_batches = 0
        self.q_send_batch_items = 0
        # Pair
        self.pair_claimed = 0
        self.pair_consumed_ok = 0
        self.pair_consumed_fail = 0
        # 成功数
        self.success_count = 0
        self.registration_starts = 0

    def next_registration_task(self):
        self.registration_starts += 1
        return self.registration_starts

    def record_success(self):
        self.success_count += 1
        self.recent_success_times.append(self._clock())

    def five_minute_success_rate(self):
        now = self._clock()
        cutoff = now - 300.0
        while self.recent_success_times and self.recent_success_times[0] < cutoff:
            self.recent_success_times.popleft()
        if not self.recent_success_times:
            return None if self.success_count == 0 else 0.0
        elapsed = max(1.0, min(300.0, now - self.started_monotonic))
        return len(self.recent_success_times) * 60.0 / elapsed

    def runtime_average_success_rate(self):
        if self.success_count == 0:
            return None
        elapsed = max(1.0, self._clock() - self.started_monotonic)
        return self.success_count * 60.0 / elapsed

    def to_dict(self, inventory=None, sems=None):
        """Structured metrics for control plane / dashboard."""
        elapsed = time.time() - self.start_time
        rate = self.success_count / (elapsed / 60) if elapsed > 60 else 0.0
        p_batch_avg = (
            self.q_send_batch_items / self.q_send_batches
            if self.q_send_batches else 0.0
        )
        t_solve_avg = (
            self.t_solve_seconds / self.t_solve_count
            if self.t_solve_count else 0.0
        )
        s_phys_wait, s_phys_hold = self._avg_pair(
            self.s_physical_wait_seconds, self.s_physical_hold_seconds, self.s_physical_count
        )
        p_phys_wait, p_phys_hold = self._avg_pair(
            self.p_physical_wait_seconds, self.p_physical_hold_seconds, self.p_physical_count
        )
        c_phys_wait, c_phys_hold = self._avg_pair(
            self.c_physical_wait_seconds, self.c_physical_hold_seconds, self.c_physical_count
        )
        data = {
            "elapsed_sec": round(elapsed, 1),
            "success_count": self.success_count,
            "registration_starts": self.registration_starts,
            "rate_per_min": round(rate, 2),
            "runtime_avg_per_min": self.runtime_average_success_rate(),
            "five_min_avg_per_min": self.five_minute_success_rate(),
            "t": {
                "depth": getattr(inventory, "t_depth", None) if inventory is not None else None,
                "produced": self.t_produced,
                "admitted": self.t_admitted,
                "claimed": self.t_claimed,
                "expired": self.t_expired,
                "discarded": self.t_discarded,
                "solve_count": self.t_solve_count,
                "solve_failed": self.t_solve_failed,
                "solve_avg_sec": round(t_solve_avg, 2),
            },
            "q": {
                "depth": getattr(inventory, "q_depth", None) if inventory is not None else None,
                "sent": self.q_sent,
                "returned": self.q_returned,
                "admitted": self.q_admitted,
                "claimed": self.q_claimed,
                "expired": self.q_expired,
                "discarded": self.q_discarded,
                "batch_avg": round(p_batch_avg, 2),
            },
            "pair": {
                "claimed": self.pair_claimed,
                "ok": self.pair_consumed_ok,
                "fail": self.pair_consumed_fail,
            },
            "stages": {
                "s_phys_wait_hold": [round(s_phys_wait, 2), round(s_phys_hold, 2)],
                "p_phys_wait_hold": [round(p_phys_wait, 2), round(p_phys_hold, 2)],
                "c_phys_wait_hold": [round(c_phys_wait, 2), round(c_phys_hold, 2)],
                "p_email_page_send": [
                    round(self._avg(self.p_email_create_seconds, self.p_email_create_count), 2),
                    round(self._avg(self.p_page_prepare_seconds, self.p_page_prepare_count), 2),
                    round(self._avg(self.p_send_seconds, self.p_send_count), 2),
                ],
                "c_page_verify_register": [
                    round(self._avg(self.c_page_acquire_seconds, self.c_page_acquire_count), 2),
                    round(self._avg(self.c_verify_seconds, self.c_verify_count), 2),
                    round(self._avg(self.c_register_seconds, self.c_register_count), 2),
                ],
            },
            "solver": {
                "goto_avg": round(self._avg(self.solver_goto_seconds, self.t_solve_count), 2),
                "inject_avg": round(self._avg(self.solver_inject_seconds, self.t_solve_count), 2),
                "initial_avg": round(self._avg(self.solver_initial_seconds, self.t_solve_count), 2),
                "click_avg": round(self._avg(self.solver_click_seconds, self.t_solve_count), 2),
                "wait_avg": round(self._avg(self.solver_wait_seconds, self.t_solve_count), 2),
                "reuse_ratio": round(
                    self.solver_reused_count / self.t_solve_count if self.t_solve_count else 0.0, 2
                ),
                "visible_ratio": round(
                    self.solver_visible_frame_count / self.t_solve_count if self.t_solve_count else 0.0, 2
                ),
            },
            "c_hot": {"hits": self.c_hot_page_hits, "misses": self.c_hot_page_misses},
        }
        if sems is not None:
            data["semaphores"] = {
                "physical": getattr(sems.get("physical"), "_value", None),
                "t_slot": getattr(sems.get("t_slot"), "_value", None),
                "q_slot": getattr(sems.get("q_slot"), "_value", None),
                "q_pending": getattr(sems.get("q_pending"), "_value", None),
                "p_send": getattr(sems.get("p_send"), "_value", None) if sems.get("p_send") else None,
            }
            admission = sems.get("admission")
            if admission is not None:
                data["admission"] = {
                    "t_in_progress": getattr(admission, "t_in_progress", None),
                    "q_inflight": getattr(admission, "q_inflight", None),
                }
        return data

    def snapshot(self, inventory, sems):
        """生成一行监控日志。"""
        d = self.to_dict(inventory, sems)
        t, q, pair = d["t"], d["q"], d["pair"]
        sem = d.get("semaphores") or {}
        adm = d.get("admission") or {}
        p_send_part = f' p_send:{sem["p_send"]}' if sem.get("p_send") is not None else ''
        admission_part = (
            f' t_prog:{adm.get("t_in_progress")} q_inflight:{adm.get("q_inflight")}'
            if adm else ''
        )
        s_w, s_h = d["stages"]["s_phys_wait_hold"]
        p_w, p_h = d["stages"]["p_phys_wait_hold"]
        c_w, c_h = d["stages"]["c_phys_wait_hold"]
        pe, pp, ps = d["stages"]["p_email_page_send"]
        cp, cv, cr = d["stages"]["c_page_verify_register"]
        sol = d["solver"]
        return (
            f'[*] T:{t["depth"]} Q:{q["depth"]} '
            f'phys:{sem.get("physical")}{p_send_part} t_slot:{sem.get("t_slot")} '
            f'q_slot:{sem.get("q_slot")} q_pend:{sem.get("q_pending")} '
            f'p_batch:{q["batch_avg"]:.1f}{admission_part} '
            f's_phys:{s_w:.2f}/{s_h:.2f} '
            f'p_phys:{p_w:.2f}/{p_h:.2f} '
            f'c_phys:{c_w:.2f}/{c_h:.2f} '
            f'p_stage:{pe:.2f}/{pp:.2f}/{ps:.2f} '
            f'c_stage:{cp:.2f}/{cv:.2f}/{cr:.2f} '
            f'c_hot:{d["c_hot"]["hits"]}/{d["c_hot"]["misses"]} '
            f't_solve_avg:{t["solve_avg_sec"]:.1f} t_solve_fail:{t["solve_failed"]} '
            f'solver_goto:{sol["goto_avg"]:.2f} solver_inject:{sol["inject_avg"]:.2f} '
            f'solver_initial:{sol["initial_avg"]:.2f} solver_click:{sol["click_avg"]:.2f} '
            f'solver_wait:{sol["wait_avg"]:.2f} solver_reuse:{sol["reuse_ratio"]:.2f} '
            f'solver_visible:{sol["visible_ratio"]:.2f} '
            f't_prod:{t["produced"]} t_adm:{t["admitted"]} t_exp:{t["expired"]} '
            f'q_sent:{q["sent"]} q_ret:{q["returned"]} q_adm:{q["admitted"]} q_exp:{q["expired"]} '
            f'pair:{pair["claimed"]} ok:{pair["ok"]} fail:{pair["fail"]} '
            f'rate:{d["rate_per_min"]:.1f}/min #{d["success_count"]}'
        )

    @staticmethod
    def _avg(total, count):
        return total / count if count else 0

    @staticmethod
    def _avg_pair(wait_total, hold_total, count):
        if not count:
            return 0, 0
        return wait_total / count, hold_total / count
