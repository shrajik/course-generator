"""The real, fixed periodic table - all 118 known elements, with their
scientifically correct atomic number, symbol, period, group and block.

This is a STATIC, hand-verified reference dataset, never re-derived or
authored by a model - see `app.schemas.diagram.DIAGRAM_KINDS`'s own
`periodic_table` comment for why: a periodic table has exactly one correct
layout, so letting an LLM or a raster image model freehand it is what
produced the real, confirmed defect this module exists to close (duplicated
element symbols, duplicated/wrong group numbers in a generated picture).

Main-grid elements (`block` in "s"/"p"/"d") carry a real `group` (1-18) and
`period` (1-7) placing them directly in the 18-column grid. Lanthanides and
actinides (`block == "f"`) are NOT placed in the main 18-column grid at all
(the conventional simplified textbook layout leaves that row's group-3 cell
empty and shows the f-block as two separate 15-wide strips below) - they
carry `period` (6 or 7, which strip) and `f_index` (1-15, position within
that strip) instead of a `group`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Element:
    atomic_number: int
    symbol: str
    name: str
    period: int
    block: str  # "s" | "p" | "d" | "f"
    group: int | None = None  # 1-18, main grid only (None for f-block)
    f_index: int | None = None  # 1-15, f-block strip position only


ELEMENTS: tuple[Element, ...] = (
    # Period 1
    Element(1, "H", "Hydrogen", 1, "s", group=1),
    Element(2, "He", "Helium", 1, "s", group=18),
    # Period 2
    Element(3, "Li", "Lithium", 2, "s", group=1),
    Element(4, "Be", "Beryllium", 2, "s", group=2),
    Element(5, "B", "Boron", 2, "p", group=13),
    Element(6, "C", "Carbon", 2, "p", group=14),
    Element(7, "N", "Nitrogen", 2, "p", group=15),
    Element(8, "O", "Oxygen", 2, "p", group=16),
    Element(9, "F", "Fluorine", 2, "p", group=17),
    Element(10, "Ne", "Neon", 2, "p", group=18),
    # Period 3
    Element(11, "Na", "Sodium", 3, "s", group=1),
    Element(12, "Mg", "Magnesium", 3, "s", group=2),
    Element(13, "Al", "Aluminium", 3, "p", group=13),
    Element(14, "Si", "Silicon", 3, "p", group=14),
    Element(15, "P", "Phosphorus", 3, "p", group=15),
    Element(16, "S", "Sulfur", 3, "p", group=16),
    Element(17, "Cl", "Chlorine", 3, "p", group=17),
    Element(18, "Ar", "Argon", 3, "p", group=18),
    # Period 4
    Element(19, "K", "Potassium", 4, "s", group=1),
    Element(20, "Ca", "Calcium", 4, "s", group=2),
    Element(21, "Sc", "Scandium", 4, "d", group=3),
    Element(22, "Ti", "Titanium", 4, "d", group=4),
    Element(23, "V", "Vanadium", 4, "d", group=5),
    Element(24, "Cr", "Chromium", 4, "d", group=6),
    Element(25, "Mn", "Manganese", 4, "d", group=7),
    Element(26, "Fe", "Iron", 4, "d", group=8),
    Element(27, "Co", "Cobalt", 4, "d", group=9),
    Element(28, "Ni", "Nickel", 4, "d", group=10),
    Element(29, "Cu", "Copper", 4, "d", group=11),
    Element(30, "Zn", "Zinc", 4, "d", group=12),
    Element(31, "Ga", "Gallium", 4, "p", group=13),
    Element(32, "Ge", "Germanium", 4, "p", group=14),
    Element(33, "As", "Arsenic", 4, "p", group=15),
    Element(34, "Se", "Selenium", 4, "p", group=16),
    Element(35, "Br", "Bromine", 4, "p", group=17),
    Element(36, "Kr", "Krypton", 4, "p", group=18),
    # Period 5
    Element(37, "Rb", "Rubidium", 5, "s", group=1),
    Element(38, "Sr", "Strontium", 5, "s", group=2),
    Element(39, "Y", "Yttrium", 5, "d", group=3),
    Element(40, "Zr", "Zirconium", 5, "d", group=4),
    Element(41, "Nb", "Niobium", 5, "d", group=5),
    Element(42, "Mo", "Molybdenum", 5, "d", group=6),
    Element(43, "Tc", "Technetium", 5, "d", group=7),
    Element(44, "Ru", "Ruthenium", 5, "d", group=8),
    Element(45, "Rh", "Rhodium", 5, "d", group=9),
    Element(46, "Pd", "Palladium", 5, "d", group=10),
    Element(47, "Ag", "Silver", 5, "d", group=11),
    Element(48, "Cd", "Cadmium", 5, "d", group=12),
    Element(49, "In", "Indium", 5, "p", group=13),
    Element(50, "Sn", "Tin", 5, "p", group=14),
    Element(51, "Sb", "Antimony", 5, "p", group=15),
    Element(52, "Te", "Tellurium", 5, "p", group=16),
    Element(53, "I", "Iodine", 5, "p", group=17),
    Element(54, "Xe", "Xenon", 5, "p", group=18),
    # Period 6
    Element(55, "Cs", "Caesium", 6, "s", group=1),
    Element(56, "Ba", "Barium", 6, "s", group=2),
    Element(57, "La", "Lanthanum", 6, "f", f_index=1),
    Element(58, "Ce", "Cerium", 6, "f", f_index=2),
    Element(59, "Pr", "Praseodymium", 6, "f", f_index=3),
    Element(60, "Nd", "Neodymium", 6, "f", f_index=4),
    Element(61, "Pm", "Promethium", 6, "f", f_index=5),
    Element(62, "Sm", "Samarium", 6, "f", f_index=6),
    Element(63, "Eu", "Europium", 6, "f", f_index=7),
    Element(64, "Gd", "Gadolinium", 6, "f", f_index=8),
    Element(65, "Tb", "Terbium", 6, "f", f_index=9),
    Element(66, "Dy", "Dysprosium", 6, "f", f_index=10),
    Element(67, "Ho", "Holmium", 6, "f", f_index=11),
    Element(68, "Er", "Erbium", 6, "f", f_index=12),
    Element(69, "Tm", "Thulium", 6, "f", f_index=13),
    Element(70, "Yb", "Ytterbium", 6, "f", f_index=14),
    Element(71, "Lu", "Lutetium", 6, "f", f_index=15),
    Element(72, "Hf", "Hafnium", 6, "d", group=4),
    Element(73, "Ta", "Tantalum", 6, "d", group=5),
    Element(74, "W", "Tungsten", 6, "d", group=6),
    Element(75, "Re", "Rhenium", 6, "d", group=7),
    Element(76, "Os", "Osmium", 6, "d", group=8),
    Element(77, "Ir", "Iridium", 6, "d", group=9),
    Element(78, "Pt", "Platinum", 6, "d", group=10),
    Element(79, "Au", "Gold", 6, "d", group=11),
    Element(80, "Hg", "Mercury", 6, "d", group=12),
    Element(81, "Tl", "Thallium", 6, "p", group=13),
    Element(82, "Pb", "Lead", 6, "p", group=14),
    Element(83, "Bi", "Bismuth", 6, "p", group=15),
    Element(84, "Po", "Polonium", 6, "p", group=16),
    Element(85, "At", "Astatine", 6, "p", group=17),
    Element(86, "Rn", "Radon", 6, "p", group=18),
    # Period 7
    Element(87, "Fr", "Francium", 7, "s", group=1),
    Element(88, "Ra", "Radium", 7, "s", group=2),
    Element(89, "Ac", "Actinium", 7, "f", f_index=1),
    Element(90, "Th", "Thorium", 7, "f", f_index=2),
    Element(91, "Pa", "Protactinium", 7, "f", f_index=3),
    Element(92, "U", "Uranium", 7, "f", f_index=4),
    Element(93, "Np", "Neptunium", 7, "f", f_index=5),
    Element(94, "Pu", "Plutonium", 7, "f", f_index=6),
    Element(95, "Am", "Americium", 7, "f", f_index=7),
    Element(96, "Cm", "Curium", 7, "f", f_index=8),
    Element(97, "Bk", "Berkelium", 7, "f", f_index=9),
    Element(98, "Cf", "Californium", 7, "f", f_index=10),
    Element(99, "Es", "Einsteinium", 7, "f", f_index=11),
    Element(100, "Fm", "Fermium", 7, "f", f_index=12),
    Element(101, "Md", "Mendelevium", 7, "f", f_index=13),
    Element(102, "No", "Nobelium", 7, "f", f_index=14),
    Element(103, "Lr", "Lawrencium", 7, "f", f_index=15),
    Element(104, "Rf", "Rutherfordium", 7, "d", group=4),
    Element(105, "Db", "Dubnium", 7, "d", group=5),
    Element(106, "Sg", "Seaborgium", 7, "d", group=6),
    Element(107, "Bh", "Bohrium", 7, "d", group=7),
    Element(108, "Hs", "Hassium", 7, "d", group=8),
    Element(109, "Mt", "Meitnerium", 7, "d", group=9),
    Element(110, "Ds", "Darmstadtium", 7, "d", group=10),
    Element(111, "Rg", "Roentgenium", 7, "d", group=11),
    Element(112, "Cn", "Copernicium", 7, "d", group=12),
    Element(113, "Nh", "Nihonium", 7, "p", group=13),
    Element(114, "Fl", "Flerovium", 7, "p", group=14),
    Element(115, "Mc", "Moscovium", 7, "p", group=15),
    Element(116, "Lv", "Livermorium", 7, "p", group=16),
    Element(117, "Ts", "Tennessine", 7, "p", group=17),
    Element(118, "Og", "Oganesson", 7, "p", group=18),
)

assert len(ELEMENTS) == 118
assert len({e.atomic_number for e in ELEMENTS}) == 118
assert len({e.symbol for e in ELEMENTS}) == 118

BY_SYMBOL: dict[str, Element] = {e.symbol.lower(): e for e in ELEMENTS}
MAIN_GRID: tuple[Element, ...] = tuple(e for e in ELEMENTS if e.block != "f")
LANTHANIDES: tuple[Element, ...] = tuple(sorted((e for e in ELEMENTS if e.period == 6 and e.block == "f"), key=lambda e: e.f_index or 0))
ACTINIDES: tuple[Element, ...] = tuple(sorted((e for e in ELEMENTS if e.period == 7 and e.block == "f"), key=lambda e: e.f_index or 0))

_BLOCK_FILL = {"s": "#fecaca", "p": "#fde68a", "d": "#bfdbfe", "f": "#ddd6fe"}


def block_fill(block: str) -> str:
    return _BLOCK_FILL.get(block, "#e5e7eb")
