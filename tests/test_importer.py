"""Run: python -m unittest discover tests   (standard library only)"""

import io
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importer  # noqa: E402

HEAD = "ref,kind,content,marks,options,answer,params,image,graph,notes\n"


def sheet(*rows):
    return (HEAD + "".join(r + "\n" for r in rows)).encode()


class TemplateTests(unittest.TestCase):
    def test_template_csv_parses_back(self):
        exam, warnings = importer.parse(importer.template_csv(), "t.csv")
        self.assertEqual(len(exam["sections"]), 1)
        self.assertEqual(len(exam["sections"][0]["questions"]), 5)
        self.assertTrue(any("picture" in w for w in warnings))

    def test_template_xlsx_is_a_real_workbook_and_matches_the_csv(self):
        raw = importer.template_xlsx()
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            self.assertIsNone(zf.testzip())
            self.assertIn("xl/worksheets/sheet1.xml", zf.namelist())
        from_xlsx, _ = importer.parse(raw, "t.xlsx")
        from_csv, _ = importer.parse(importer.template_csv(), "t.csv")
        self.assertEqual(from_xlsx, from_csv)

    def test_preamble_tells_an_ai_what_it_is_for(self):
        text = importer.template_csv().decode("utf-8-sig").lower()
        self.assertIn("ai assistant", text)
        self.assertIn("constraints", text)


class StructureTests(unittest.TestCase):
    def test_questions_parts_and_subparts_nest(self):
        exam, _ = importer.parse(sheet(
            "A,section,Short answer,,,,to_answer=2,,,",
            ",question,Stem,5,,,,,,",
            ",part,First,3,,,,,,",
            ",subpart,Deeper,1,,,,,,",
            ",question,Second,4,,,,,,",
        ), "x.csv")
        s = exam["sections"][0]
        self.assertEqual((s["name"], s["description"], s["to_answer"]), ("A", "Short answer", "2"))
        self.assertEqual(len(s["questions"]), 2)
        self.assertEqual(s["questions"][0]["marks"], 5)
        self.assertEqual(s["questions"][0]["parts"][0]["parts"][0]["marks"], 1)

    def test_questions_without_a_section_get_one(self):
        exam, _ = importer.parse(sheet(",question,Stem,2,,,,,,"), "x.csv")
        self.assertEqual(exam["sections"][0]["name"], "A")

    def test_blocks_attach_to_the_deepest_item(self):
        exam, _ = importer.parse(sheet(
            ",question,Stem,4,,,,,,",
            ",part,Part,2,,,,,,",
            ",lines,,,,,lines=6,,,",
        ), "x.csv")
        part = exam["sections"][0]["questions"][0]["parts"][0]
        self.assertEqual(part["blocks"][-1], {"type": "lines", "n": 6})

    def test_every_block_kind_converts(self):
        exam, warnings = importer.parse(sheet(
            ",question,Stem,9,,,,,,",
            ",text,Some words,,,,,,,",
            ",equation,\\frac{1}{2}mv^2,,,,,,,",
            ",mc,,,A | B | C,B,,,,",
            ",table,a|b; c|d,,,,header=yes; shade_cols=2,,,",
            ",answer,,,,,labels=v|t; units=m s^-1|s,,,",
            ",lines,,,,,lines=3,,,",
            ",box,,,,,height_cm=8,,,",
            ",image,,,,,width=50,a ray diagram,,",
            ",graph,,,,,,,x=0..10 step 2 label t; y=0..5; fn=x^2 dashed; points=(1 2),",
        ), "x.csv")
        blocks = exam["sections"][0]["questions"][0]["blocks"]
        kinds = [b["type"] for b in blocks]
        self.assertEqual(kinds, ["text", "text", "equation", "choices", "table", "answer",
                                 "lines", "box", "image", "graph"])
        by = {b["type"]: b for b in blocks}
        self.assertEqual(by["choices"]["correct"], 1)
        self.assertEqual(by["table"]["shade_cols"], [1])
        self.assertEqual([x["units"] for x in by["answer"]["boxes"]], ["m s^-1", "s"])
        self.assertEqual(by["box"]["height_cm"], 8)
        self.assertEqual((by["image"]["value"], by["image"]["placeholder"]), ("", "a ray diagram"))
        self.assertTrue(warnings)
        g = by["graph"]
        self.assertEqual((g["x"]["min"], g["x"]["max"], g["x"]["step"], g["x"]["label"]), (0, 10, 2, "t"))
        self.assertEqual(g["functions"], [{"expr": "x^2", "from": None, "to": None, "dashed": True}])
        self.assertEqual(g["points"], [{"x": 1.0, "y": 2.0, "label": "", "open": False}])

    def test_alignment_and_new_page(self):
        exam, _ = importer.parse(sheet(
            ",question,Stem,2,,,new_page=yes,,,",
            ",table,a|b,,,,align=left,,,",
        ), "x.csv")
        q = exam["sections"][0]["questions"][0]
        self.assertTrue(q["new_page"])
        self.assertEqual(q["blocks"][-1]["align"], "l")

    def test_boxplot_and_histogram(self):
        exam, _ = importer.parse(sheet(
            ",question,Stem,2,,,,,,",
            ",graph,,,,,,,boxplot=2 6 9 13 18,",
            ",graph,,,,,,,histogram=2 5 8 4 1,",
        ), "x.csv")
        blocks = exam["sections"][0]["questions"][0]["blocks"]
        self.assertEqual(blocks[1]["kind"], "boxplot")
        self.assertEqual(blocks[1]["box"]["median"], 9)
        self.assertEqual(blocks[2]["kind"], "histogram")

    def test_commas_still_work_when_the_cell_survives_intact(self):
        """Excel and any quoting writer keep commas, so both forms must parse the same."""
        spaced, _ = importer.parse(sheet(",question,S,1,,,,,,", ",graph,,,,,,,points=(1 2)(3 4),"), "x.csv")
        quoted, _ = importer.parse(
            (HEAD + ',question,S,1,,,,,,\n,graph,,,,,,,"points=(1,2),(3,4)",\n').encode(), "x.csv")
        self.assertEqual(spaced["sections"][0]["questions"][0]["blocks"][1]["points"],
                         quoted["sections"][0]["questions"][0]["blocks"][1]["points"])

    def test_a_comma_split_graph_cell_fails_loudly_rather_than_losing_data(self):
        """A .csv comma cuts the cell short; that must be an error, never a silently empty graph."""
        for spec in ("points=(1", "boxplot=2", "histogram="):
            with self.assertRaises(importer.SheetError) as caught:
                importer.parse(sheet(",question,S,1,,,,,,", f",graph,,,,,,,{spec},"), "x.csv")
            self.assertIn("Row 3", " ".join(caught.exception.problems))

    def test_comment_rows_and_blank_rows_are_skipped(self):
        exam, _ = importer.parse(sheet(
            ",#,ignore me,,,,,,,",
            ",,,,,,,,,",
            ",question,Stem,1,,,,,,",
        ), "x.csv")
        self.assertEqual(len(exam["sections"][0]["questions"]), 1)

    def test_defaults_are_carried_into_the_exam(self):
        exam, _ = importer.parse(sheet(",question,Stem,1,,,,,,"), "x.csv",
                                 defaults={"task": "Unit 2 Test", "subject": "Physics"})
        self.assertEqual((exam["task"], exam["subject"]), ("Unit 2 Test", "Physics"))


class RejectionTests(unittest.TestCase):
    def bad(self, *rows, text=None):
        with self.assertRaises(importer.SheetError) as caught:
            importer.parse(text.encode() if text else sheet(*rows), "x.csv")
        return " ".join(caught.exception.problems)

    def test_unknown_kind_names_the_row(self):
        self.assertIn("Row 3", self.bad(",question,Stem,1,,,,,,", ",wobble,x,,,,,,,"))

    def test_orphan_part_and_orphan_block(self):
        self.assertIn("needs a question above it", self.bad(",part,orphan,1,,,,,,"))
        self.assertIn("needs a question above it", self.bad(",lines,,,,,lines=3,,,"))

    def test_bad_marks_options_and_answer(self):
        self.assertIn("between 0 and 999", self.bad(",question,Stem,4000,,,,,,"))
        self.assertIn("2 to 8 options", self.bad(",question,S,1,,,,,,", ",mc,,,only one,,,,,"))
        self.assertIn("option letter", self.bad(",question,S,1,,,,,,", ",mc,,,A | B,Q,,,,"))

    def test_unknown_param_lists_what_is_allowed(self):
        self.assertIn("unknown param", self.bad(",question,S,1,,,,,,", ",lines,,,,,wibble=3,,,"))

    def test_missing_header_and_empty_sheet(self):
        self.assertIn("No header row", self.bad(text="just,some,notes\n"))
        self.assertIn("no questions", self.bad(text=HEAD + "A,section,Empty,,,,,,,\n"))

    def test_a_sheet_that_is_not_a_spreadsheet(self):
        with self.assertRaises(importer.SheetError):
            importer.parse(b"PK\x03\x04 not really a zip", "x.xlsx")

    def test_oversized_table_is_refused(self):
        wide = "|".join(str(i) for i in range(20))
        self.assertIn("at most", self.bad(",question,S,1,,,,,,", f",table,{wide},,,,,,,"))


if __name__ == "__main__":
    unittest.main()
