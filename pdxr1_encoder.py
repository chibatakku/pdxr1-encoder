"""
PDXR1/M + PDXC2 エンコーダ値の取得・記録スクリプト（Kinesis + pythonnet）

公式サンプル Thorlabs/Motion_Control_Examples
  Python/Kinesis/Benchtop/PDXC2/PDXC2_pythonnet.py をもとに作成。

使い方（PowerShell、Kinesis GUI は閉じておく）:
  uv run pdxr1_encoder.py

単位:
  回転ステージでは 360° = 14,400,000 エンコーダカウント
  → 1 カウント = 0.000025° (= 0.09 arcsec = 0.4363 µrad)
"""

import csv
import os
import time
from datetime import datetime

# ===== 設定 ==============================================================
SERIAL = "112569560"        # PDXC2 のシリアル番号
KINESIS = r"C:\Program Files\Thorlabs\Kinesis"

# Kinesis の内部 DLL（USB 通信用など）を見つけられるようにする
os.add_dll_directory(KINESIS)
os.environ["PATH"] = KINESIS + ";" + os.environ["PATH"]

import clr  # noqa: E402

POLL_MS = 50                # 位置（ステータス）の更新間隔 [ms]
DO_OPTIMIZE = False         # True: Performance Optimize を実行（負荷を変えたとき）
SPEED_DEG_S = 20.0          # 閉ループ移動速度 [°/s]（推奨 10〜30）
ACCEL_DEG_S2 = 250.0        # 閉ループ加速度 [°/s²]（公式サンプル相当）
THETA0_DEG = 0.0            # 基準オフセット [°]（穴の並び・ミラー基準など）

MODE = "steps"         # "continuous" または "steps"
DURATION_S = 5.0            # continuous: 記録時間 [s]
STEP_TARGETS_DEG = [0.0, 10.0] * 10   # steps: 目標角の列（例：0→10°を10往復）
SAMPLES_PER_TARGET = 20     # steps: 各目標で読む回数
SETTLE_S = 1.0              # steps: 到達後の整定待ち [s]
# ========================================================================

COUNTS_PER_REV = 14_400_000
DEG_PER_COUNT = 360.0 / COUNTS_PER_REV
ARCSEC_PER_DEG = 3600.0
ARCSEC_PER_COUNT = DEG_PER_COUNT * ARCSEC_PER_DEG   # = 0.09 arcsec

clr.AddReference(KINESIS + r"\Thorlabs.MotionControl.DeviceManagerCLI.dll")
clr.AddReference(KINESIS + r"\Thorlabs.MotionControl.GenericPiezoCLI.dll")
clr.AddReference(KINESIS + r"\Thorlabs.MotionControl.Benchtop.PiezoCLI.dll")

from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI, DeviceConfiguration  # noqa: E402  # type: ignore
from Thorlabs.MotionControl.Benchtop.PiezoCLI.PDXC2 import InertiaStageController  # noqa: E402  # type: ignore
from Thorlabs.MotionControl.GenericPiezoCLI.Piezo import PiezoControlModeTypes  # noqa: E402  # type: ignore


def deg_to_counts(deg: float) -> int:
    return int(round(deg / DEG_PER_COUNT))


def counts_to_deg(counts: int) -> float:
    return counts * DEG_PER_COUNT


def counts_to_rel_arcsec(counts: int) -> float:
    """基準オフセット THETA0_DEG からの相対角 [arcsec]"""
    return counts * ARCSEC_PER_COUNT - THETA0_DEG * ARCSEC_PER_DEG


def read_counts(dev) -> int:
    """エンコーダ値（カウント）。値はポーリングごとに更新される。"""
    return int(dev.GetCurrentPosition())


def connect():
    DeviceManagerCLI.BuildDeviceList()
    found = list(DeviceManagerCLI.GetDeviceList(InertiaStageController.DevicePrefix_PDXC2))
    if SERIAL not in found:
        raise RuntimeError(f"PDXC2 {SERIAL} が見つかりません。検出: {found}")

    dev = InertiaStageController.CreateInertiaStageController(SERIAL)
    dev.Connect(SERIAL)
    if not dev.IsSettingsInitialized():
        dev.WaitForSettingsInitialized(10000)
    dev.StartPolling(POLL_MS)
    time.sleep(0.5)
    dev.EnableDevice()
    time.sleep(0.5)
    # 設定の読み込み（公式サンプルと同じ手順）
    dev.GetPDXC2Configuration(SERIAL, DeviceConfiguration.DeviceSettingsUseOptionType.UseDeviceSettings)
    print("Connected:", dev.GetDeviceInfo().Description)
    return dev


def setup_closed_loop(dev):
    dev.SetPositionControlMode(PiezoControlModeTypes.CloseLoop)
    time.sleep(0.5)

    if DO_OPTIMIZE:
        print("Performance Optimize 実行中（1〜2分）...")
        dev.PulseParamsAcquireStart()
        time.sleep(0.5)
        while not dev.Status.IsPulseParamsAcquired:
            time.sleep(0.5)

    print("Homing...")
    dev.Home(60000)                       # 完了まで待つ（タイムアウト 60 s）
    print(f"Homed. {counts_to_deg(read_counts(dev)):.6f} deg")

    p = dev.GetClosedLoopParameters()
    p.RefSpeed = deg_to_counts(SPEED_DEG_S)        # [EnCnt/s]
    p.Acceleration = deg_to_counts(ACCEL_DEG_S2)   # [EnCnt/s²]
    dev.SetClosedLoopParameters(p)


def _command_move(dev, target):
    dev.SetClosedLoopTarget(target)
    time.sleep(0.1)                       # 目標設定が反映されてから開始する
    dev.MoveStart()


def move_to_deg(dev, target_deg, tol_counts=40, timeout_s=30.0,
                retry_s=2.0, max_retries=3):
    """絶対角へ移動し、目標近傍で値が落ち着くまで待つ。tol 40カウント = 0.001° = 3.6 arcsec

    - すでに目標近傍なら移動指令を出さない（ゼロ距離移動で制御器が待ち状態になるのを防ぐ）
    - 指令後 retry_s 秒たっても動き出さなければ、指令を再送する
    """
    target = deg_to_counts(target_deg + THETA0_DEG)
    start = read_counts(dev)
    if abs(start - target) <= tol_counts:
        return True

    _command_move(dev, target)
    t0 = t_cmd = time.time()
    stable, retries = 0, 0
    while time.time() - t0 < timeout_s:
        time.sleep(POLL_MS / 1000)
        c = read_counts(dev)
        if abs(c - target) <= tol_counts:
            stable += 1
            if stable >= 4:
                return True
            continue
        stable = 0
        not_started = abs(c - start) <= tol_counts
        if not_started and time.time() - t_cmd > retry_s and retries < max_retries:
            retries += 1
            print(f"  移動が始まらないため指令を再送します（{retries}/{max_retries}）")
            _command_move(dev, target)
            t_cmd = time.time()
    print(f"警告: {target_deg}° に到達しませんでした（現在 {counts_to_deg(read_counts(dev)):.5f}°）")
    return False


def stamp():
    return datetime.now().isoformat(timespec="milliseconds")


def log_continuous(dev, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wallclock", "t_s", "counts", "angle_deg", "angle_rel_deg", "angle_rel_arcsec"])
        t0 = time.perf_counter()
        while (t := time.perf_counter() - t0) < DURATION_S:
            c = read_counts(dev)
            a = counts_to_deg(c)
            w.writerow([stamp(), f"{t:.4f}", c, f"{a:.6f}", f"{a - THETA0_DEG:.6f}",
                        f"{counts_to_rel_arcsec(c):.2f}"])
            time.sleep(POLL_MS / 1000)


def log_steps(dev, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wallclock", "index", "target_deg", "target_arcsec", "sample", "counts",
                    "angle_rel_deg", "angle_rel_arcsec", "reached"])
        for i, tgt in enumerate(STEP_TARGETS_DEG):
            ok = move_to_deg(dev, tgt)
            time.sleep(SETTLE_S)
            for s in range(SAMPLES_PER_TARGET):
                c = read_counts(dev)
                w.writerow([stamp(), i, tgt, f"{tgt * ARCSEC_PER_DEG:.2f}", s, c,
                            f"{counts_to_deg(c) - THETA0_DEG:.6f}",
                            f"{counts_to_rel_arcsec(c):.2f}", int(ok)])
                time.sleep(POLL_MS / 1000)
            print(f"[{i + 1}/{len(STEP_TARGETS_DEG)}] target {tgt}°")


def main():
    dev = connect()
    try:
        setup_closed_loop(dev)
        path = f"pdxr1_{MODE}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        if MODE == "continuous":
            log_continuous(dev, path)
        elif MODE == "steps":
            log_steps(dev, path)
        else:
            raise ValueError(MODE)
        print("Saved:", path)
    finally:
        dev.StopPolling()
        dev.Disconnect(True)
        print("Disconnected.")


if __name__ == "__main__":
    main()