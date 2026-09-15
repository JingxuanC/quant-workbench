"""数据加载：把 daily_pv_all.h5 读成闸门要的 (close, volume) 宽表。

只在需要的时间窗内 unstack —— 全历史 unstack 会吃掉几百 MB（4302×6082 两遍）。
"""

import pandas as pd


def load_h5(h5_path, start: str = "2024-01-01", end: str = None):
    """→ (close: dates×instruments, volume: 同形)。$close 已在 h5 里做过后复权。"""
    df = pd.read_hdf(str(h5_path), key="data")
    d = df.index.get_level_values("datetime")
    mask = d >= pd.Timestamp(start)
    if end:
        mask &= d <= pd.Timestamp(end)
    sub = df[mask]
    close = sub["$close"].unstack("instrument")
    volume = sub["$volume"].unstack("instrument")
    return close.sort_index(), volume.sort_index()
