"""
The editor's three-way merge (mergeExam in static/index.html), run with Node.

Run: python -m unittest discover tests   (skipped when Node.js isn't installed)
"""

import copy
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
MERGE_JS = re.search(r"^// -+ merging .*?^// -+ end of merging", HTML, re.S | re.M).group(0)


def run_merge(base, mine, theirs):
    """(merged mine, report) as the editor would produce them."""
    script = MERGE_JS + f"""
const mine = {json.dumps(mine)};
const report = mergeExam({json.dumps(base)}, mine, {json.dumps(theirs)});
console.log(JSON.stringify({{ mine, conflicts: report.conflicts, changed: [...report.changed].sort(), whole: report.whole }}));"""
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    return out["mine"], out


def q(id, text, marks=1, parts=None):
    return {"id": id, "marks": marks, "blocks": [{"type": "text", "value": text}], "parts": parts or []}


def exam(*questions, **cover):
    return {"unit": "Algebra", "subject": "Maths", **cover,
            "sections": [{"id": "s1", "name": "A", "questions": list(questions)}]}


def texts(ex, section=0):
    return [x["blocks"][0]["value"] for x in ex["sections"][section]["questions"]]


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class MergeTests(unittest.TestCase):
    def setUp(self):
        self.base = exam(q("a", "one"), q("b", "two"), q("c", "three"))

    def edit(self, ex, i, text):
        ex = copy.deepcopy(ex)
        ex["sections"][0]["questions"][i]["blocks"][0]["value"] = text
        return ex

    def test_nothing_changed_here_takes_theirs(self):
        theirs = self.edit(self.base, 1, "two, by Claude")
        merged, r = run_merge(self.base, copy.deepcopy(self.base), theirs)
        self.assertEqual(texts(merged), ["one", "two, by Claude", "three"])
        self.assertEqual((r["conflicts"], r["changed"]), (0, ["b"]))

    def test_different_questions_both_kept(self):
        mine = self.edit(self.base, 0, "one, by teacher")
        theirs = self.edit(self.base, 2, "three, by Claude")
        merged, r = run_merge(self.base, mine, theirs)
        self.assertEqual(texts(merged), ["one, by teacher", "two", "three, by Claude"])
        self.assertEqual(r["conflicts"], 0)

    def test_same_question_teacher_wins(self):
        mine = self.edit(self.base, 1, "teacher's")
        theirs = self.edit(self.base, 1, "Claude's")
        merged, r = run_merge(self.base, mine, theirs)
        self.assertEqual(texts(merged), ["one", "teacher's", "three"])
        self.assertEqual((r["conflicts"], r["changed"]), (1, []))

    def test_same_question_different_fields_both_kept(self):
        mine = self.edit(self.base, 1, "teacher's wording")
        theirs = copy.deepcopy(self.base)
        theirs["sections"][0]["questions"][1]["marks"] = 3
        merged, r = run_merge(self.base, mine, theirs)
        self.assertEqual((merged["sections"][0]["questions"][1]["marks"], texts(merged)[1]), (3, "teacher's wording"))
        self.assertEqual(r["conflicts"], 0)

    def test_claude_adds_while_teacher_edits(self):
        mine = self.edit(self.base, 0, "one, by teacher")
        theirs = copy.deepcopy(self.base)
        theirs["sections"][0]["questions"].insert(1, q("new1", "inserted"))
        theirs["sections"][0]["questions"].append(q("new2", "appended"))
        merged, r = run_merge(self.base, mine, theirs)
        self.assertEqual(texts(merged), ["one, by teacher", "inserted", "two", "three", "appended"])
        self.assertEqual(r["changed"], ["new1", "new2"])

    def test_both_add(self):
        mine = copy.deepcopy(self.base)
        mine["sections"][0]["questions"].insert(1, q("mine", "teacher added"))
        theirs = copy.deepcopy(self.base)
        theirs["sections"][0]["questions"].append(q("theirs", "Claude added"))
        merged, _ = run_merge(self.base, mine, theirs)
        self.assertEqual(texts(merged), ["one", "teacher added", "two", "three", "Claude added"])

    def test_claude_reorders_teacher_edits(self):
        mine = self.edit(self.base, 1, "two, by teacher")
        theirs = copy.deepcopy(self.base)
        theirs["sections"][0]["questions"].reverse()
        merged, r = run_merge(self.base, mine, theirs)
        self.assertEqual(texts(merged), ["three", "two, by teacher", "one"])
        self.assertEqual(r["conflicts"], 0)

    def test_both_reorder_teacher_order_wins(self):
        mine = copy.deepcopy(self.base)
        qs = mine["sections"][0]["questions"]
        qs.append(qs.pop(0))  # two three one
        theirs = copy.deepcopy(self.base)
        theirs["sections"][0]["questions"].reverse()  # three two one
        merged, _ = run_merge(self.base, mine, theirs)
        self.assertEqual(texts(merged), ["two", "three", "one"])

    def test_deletes(self):
        theirs = copy.deepcopy(self.base)
        del theirs["sections"][0]["questions"][2]
        merged, r = run_merge(self.base, copy.deepcopy(self.base), theirs)
        self.assertEqual(texts(merged), ["one", "two"])
        self.assertEqual(r["changed"], ["c"])

        mine = copy.deepcopy(self.base)
        del mine["sections"][0]["questions"][0]
        merged, _ = run_merge(self.base, mine, self.base)
        self.assertEqual(texts(merged), ["two", "three"])  # deleted here stays deleted

    def test_delete_versus_edit_keeps_teacher_side(self):
        mine = self.edit(self.base, 0, "edited by teacher")
        theirs = copy.deepcopy(self.base)
        del theirs["sections"][0]["questions"][0]
        merged, r = run_merge(self.base, mine, theirs)
        self.assertEqual(texts(merged)[0], "edited by teacher")
        self.assertEqual(r["conflicts"], 1)

        mine = copy.deepcopy(self.base)
        del mine["sections"][0]["questions"][0]
        theirs = self.edit(self.base, 0, "edited by Claude")
        merged, r = run_merge(self.base, mine, theirs)
        self.assertEqual(texts(merged), ["two", "three"])
        self.assertEqual(r["conflicts"], 1)

    def test_parts_merge_separately(self):
        base = exam(q("a", "stem", marks=None, parts=[q("a1", "part a"), q("a2", "part b")]))
        mine = copy.deepcopy(base)
        mine["sections"][0]["questions"][0]["parts"][0]["blocks"][0]["value"] = "part a, teacher"
        theirs = copy.deepcopy(base)
        theirs["sections"][0]["questions"][0]["parts"][1]["marks"] = 4
        theirs["sections"][0]["questions"][0]["parts"].append(q("a3", "part c"))
        merged, r = run_merge(base, mine, theirs)
        parts = merged["sections"][0]["questions"][0]["parts"]
        self.assertEqual([p["blocks"][0]["value"] for p in parts], ["part a, teacher", "part b", "part c"])
        self.assertEqual(parts[1]["marks"], 4)
        self.assertEqual((r["conflicts"], r["changed"]), (0, ["a2", "a3"]))

    def test_cover_fields_and_sections(self):
        mine = copy.deepcopy(self.base)
        mine["unit"] = "Quadratics"
        theirs = copy.deepcopy(self.base)
        theirs["writing_min"] = 90
        theirs["sections"][0]["description"] = "Short answer"
        theirs["sections"].append({"id": "s2", "name": "B", "questions": [q("d", "four")]})
        merged, r = run_merge(self.base, mine, theirs)
        self.assertEqual((merged["unit"], merged["writing_min"]), ("Quadratics", 90))
        self.assertEqual([s["name"] for s in merged["sections"]], ["A", "B"])
        self.assertEqual(merged["sections"][0]["description"], "Short answer")
        self.assertEqual(r["changed"], ["cover", "s1", "s2"])

    def test_question_moved_between_sections(self):
        base = copy.deepcopy(self.base)
        base["sections"].append({"id": "s2", "name": "B", "questions": [q("d", "four")]})
        theirs = copy.deepcopy(base)
        theirs["sections"][1]["questions"].append(theirs["sections"][0]["questions"].pop(0))
        merged, _ = run_merge(base, copy.deepcopy(base), theirs)
        self.assertEqual((texts(merged, 0), texts(merged, 1)), (["two", "three"], ["four", "one"]))

    def test_key_order_does_not_count_as_a_change(self):
        theirs = copy.deepcopy(self.base)
        theirs["sections"][0]["questions"][0] = {"parts": [], "blocks": [{"value": "one", "type": "text"}], "marks": 1, "id": "a"}
        _, r = run_merge(self.base, copy.deepcopy(self.base), theirs)
        self.assertEqual((r["conflicts"], r["changed"]), (0, []))

    def test_without_shared_ids_theirs_is_taken_whole(self):
        old = exam(q(None, "one"))
        old["sections"][0].pop("id")
        theirs = exam(q("x", "Claude's version"))
        merged, r = run_merge(old, exam(q("y", "teacher's")), theirs)
        self.assertTrue(r["whole"])
        self.assertEqual(texts(merged), ["Claude's version"])

    def test_merge_works_in_place(self):
        """The question being typed into must stay the same object."""
        script = MERGE_JS + """
const base = %s, theirs = %s, mine = structuredClone(base);
const typing = mine.sections[0].questions[0];
typing.blocks[0].value = "still typing";
mergeExam(base, mine, theirs);
console.log(JSON.stringify([mine.sections[0].questions[0] === typing, mine.sections[0].questions.length]));""" % (
            json.dumps(self.base), json.dumps(self.edit(self.base, 2, "Claude")))
        same_object, count = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True,
                                                       check=True).stdout)
        self.assertEqual((same_object, count), (True, 3))


if __name__ == "__main__":
    unittest.main()
