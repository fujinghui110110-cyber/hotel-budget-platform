import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from budgeting.excel.ooxml import (
    DRAWING_MAIN_NS,
    DRAWING_NS,
    repair_drawing_namespaces,
)


def _drawing_xml(*, malformed_prst_geom=False, unrelated_malformed=False):
    prst_geom_child = "<avLst/>" if malformed_prst_geom else "<a:avLst/>"
    unrelated_child = "<avLst/>" if unrelated_malformed else "<a:avLst/>"
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<wsDr xmlns="{DRAWING_NS}">'
        "<twoCellAnchor><pic><spPr>"
        f'<a:prstGeom xmlns:a="{DRAWING_MAIN_NS}" prst="rect">'
        f"{prst_geom_child}</a:prstGeom>"
        f'<a:ln xmlns:a="{DRAWING_MAIN_NS}">{unrelated_child}</a:ln>'
        "</spPr></pic></twoCellAnchor>"
        "</wsDr>"
    ).encode("utf-8")


def _write_package(path, *, drawing1, drawing2=None):
    entries = {
        "[Content_Types].xml": b"content-types",
        "xl/workbook.xml": b"workbook",
        "xl/worksheets/sheet1.xml": b"sheet",
        "xl/drawings/drawing1.xml": drawing1,
    }
    if drawing2 is not None:
        entries["xl/drawings/drawing2.xml"] = drawing2

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.comment = b"drawing namespace test"
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 2, 3, 4, 5))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            package.writestr(info, data)


def _entry_snapshot(path):
    with zipfile.ZipFile(path) as package:
        return {
            info.filename: (
                package.read(info),
                info.date_time,
                info.compress_type,
                info.external_attr,
                info.flag_bits,
                info.extra,
                info.comment,
            )
            for info in package.infolist()
        }


class DrawingNamespaceRepairTests(unittest.TestCase):
    def test_repairs_only_misnamespaced_prst_geom_avlst(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "drawing.xlsx"
            drawing2 = _drawing_xml(
                malformed_prst_geom=False,
                unrelated_malformed=True,
            )
            _write_package(
                path,
                drawing1=_drawing_xml(
                    malformed_prst_geom=True,
                    unrelated_malformed=True,
                ),
                drawing2=drawing2,
            )
            before = _entry_snapshot(path)

            repaired = repair_drawing_namespaces(path)

            self.assertEqual(repaired, ["xl/drawings/drawing1.xml"])
            with zipfile.ZipFile(path) as package:
                root = ET.fromstring(package.read("xl/drawings/drawing1.xml"))
                fixed = root.find(
                    f".//{{{DRAWING_MAIN_NS}}}prstGeom/{{{DRAWING_MAIN_NS}}}avLst"
                )
                unrelated = root.find(
                    f".//{{{DRAWING_MAIN_NS}}}ln/{{{DRAWING_NS}}}avLst"
                )
                self.assertIsNotNone(fixed)
                self.assertIsNotNone(unrelated)
                self.assertEqual(package.comment, b"drawing namespace test")
                self.assertEqual(
                    package.read("xl/drawings/drawing2.xml"),
                    before["xl/drawings/drawing2.xml"][0],
                )

                for info in package.infolist():
                    if info.filename == "xl/drawings/drawing1.xml":
                        continue
                    old = before[info.filename]
                    self.assertEqual(package.read(info), old[0])
                    self.assertEqual(info.date_time, old[1])
                    self.assertEqual(info.compress_type, old[2])
                    self.assertEqual(info.external_attr, old[3])
                    self.assertEqual(info.flag_bits, old[4])
                    self.assertEqual(info.extra, old[5])
                    self.assertEqual(info.comment, old[6])

    def test_correct_drawing_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "drawing.xlsx"
            _write_package(path, drawing1=_drawing_xml())
            before = path.read_bytes()

            self.assertEqual(repair_drawing_namespaces(path), [])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(repair_drawing_namespaces(path), [])
            self.assertEqual(path.read_bytes(), before)

    def test_unrelated_avlst_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "drawing.xlsx"
            _write_package(
                path,
                drawing1=_drawing_xml(
                    malformed_prst_geom=False,
                    unrelated_malformed=True,
                ),
            )
            before = path.read_bytes()

            self.assertEqual(repair_drawing_namespaces(path), [])
            self.assertEqual(path.read_bytes(), before)

    def test_failed_atomic_replace_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "drawing.xlsx"
            _write_package(
                path,
                drawing1=_drawing_xml(malformed_prst_geom=True),
            )
            before = path.read_bytes()

            with patch(
                "budgeting.excel.ooxml.os.replace",
                side_effect=OSError("simulated replace failure"),
            ):
                with self.assertRaises(OSError):
                    repair_drawing_namespaces(path)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
