"""A user year under another index relabels the SOURCE of every shipped CPI-U year."""
from fx import *

if __name__ == "__main__":
    cfg, store = mk()
    from mrfx.inflation import save_index_values, load_index, deflator, erosion_line, real_change_pct
    save_index_values(store, [(2025, 120.0)], index_name="MEI", source="CMS Medicare Economic Index 2025")
    idx = load_index(store)
    d = deflator(idx, "2023-06", "2024-06")
    print("index_name:", idx["index_name"], "| 2023-2024 deflator basis:", d["basis"], d.get("index_name"), "| source:", d.get("source"))
    print(erosion_line("P Health", 0.0, real_change_pct(0.0, d), "2023-06", "2024-06", d))
