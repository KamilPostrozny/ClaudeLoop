import unittest

from claudeloop.render import PREVIEW_CHARS, render_event


def assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def user(*blocks):
    return {"type": "user", "message": {"role": "user", "content": list(blocks)}}


class RenderTextTest(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(
            render_event(assistant({"type": "text", "text": "Working on it."})),
            [{"kind": "text", "text": "Working on it."}],
        )

    def test_blank_text_produces_nothing(self):
        self.assertEqual(render_event(assistant({"type": "text", "text": "  \n "})), [])

    def test_text_is_stripped(self):
        entries = render_event(assistant({"type": "text", "text": "  hi  "}))
        self.assertEqual(entries[0]["text"], "hi")


class RenderToolTest(unittest.TestCase):
    def test_tool_use_summarises_its_first_recognisable_argument(self):
        entries = render_event(
            assistant(
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Edit",
                    "input": {"file_path": "/repo/src/foo.py", "old_string": "a"},
                }
            )
        )
        self.assertEqual(
            entries, [{"kind": "tool", "id": "toolu_1", "name": "Edit", "summary": "/repo/src/foo.py"}]
        )

    def test_bash_summarises_its_command(self):
        entries = render_event(
            assistant({"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "npm test"}})
        )
        self.assertEqual(entries[0]["summary"], "npm test")

    def test_a_newline_in_the_summary_becomes_a_space(self):
        entries = render_event(
            assistant({"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "a\nb"}})
        )
        self.assertEqual(entries[0]["summary"], "a b")

    def test_an_unrecognised_input_summarises_to_empty(self):
        entries = render_event(
            assistant({"type": "tool_use", "id": "t", "name": "Mystery", "input": {"zzz": 1}})
        )
        self.assertEqual(entries[0]["summary"], "")

    def test_a_missing_input_does_not_raise(self):
        entries = render_event(assistant({"type": "tool_use", "id": "t", "name": "X"}))
        self.assertEqual(entries[0], {"kind": "tool", "id": "t", "name": "X", "summary": ""})

    def test_prose_and_tool_calls_in_one_message_all_survive(self):
        entries = render_event(
            assistant(
                {"type": "text", "text": "Editing two files."},
                {"type": "tool_use", "id": "a", "name": "Edit", "input": {"file_path": "one.py"}},
                {"type": "tool_use", "id": "b", "name": "Edit", "input": {"file_path": "two.py"}},
            )
        )
        self.assertEqual([e["kind"] for e in entries], ["text", "tool", "tool"])
        self.assertEqual([e.get("summary") for e in entries[1:]], ["one.py", "two.py"])


class RenderResultTest(unittest.TestCase):
    def test_a_string_tool_result(self):
        entries = render_event(
            user({"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"})
        )
        self.assertEqual(
            entries, [{"kind": "result", "id": "toolu_1", "preview": "ok", "is_error": False}]
        )

    def test_a_block_list_tool_result_is_flattened(self):
        entries = render_event(
            user(
                {
                    "type": "tool_result",
                    "tool_use_id": "t",
                    "content": [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}],
                }
            )
        )
        self.assertEqual(entries[0]["preview"], "line one\nline two")

    def test_a_long_result_is_clipped(self):
        entries = render_event(
            user({"type": "tool_result", "tool_use_id": "t", "content": "x" * 5000})
        )
        self.assertEqual(len(entries[0]["preview"]), PREVIEW_CHARS)
        self.assertTrue(entries[0]["preview"].endswith("…"))

    def test_an_error_result_is_flagged(self):
        entries = render_event(
            user({"type": "tool_result", "tool_use_id": "t", "content": "boom", "is_error": True})
        )
        self.assertTrue(entries[0]["is_error"])


class RenderOtherTest(unittest.TestCase):
    def test_the_result_event(self):
        entries = render_event(
            {
                "type": "result",
                "subtype": "success",
                "total_cost_usd": 0.0248249,
                "duration_ms": 5587,
            }
        )
        self.assertEqual(entries[0]["kind"], "done")
        self.assertAlmostEqual(entries[0]["cost"], 0.0248249)
        self.assertEqual(entries[0]["duration_ms"], 5587)
        self.assertEqual(entries[0]["subtype"], "success")

    def test_a_result_event_missing_its_numbers(self):
        entries = render_event({"type": "result"})
        self.assertEqual(entries[0]["cost"], 0.0)
        self.assertEqual(entries[0]["duration_ms"], 0)

    def test_rate_limit_events_are_not_transcript_entries(self):
        self.assertEqual(
            render_event(
                {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1}}
            ),
            [],
        )

    def test_system_events_are_ignored(self):
        self.assertEqual(render_event({"type": "system", "subtype": "init"}), [])

    def test_a_message_with_no_content_list_is_ignored(self):
        self.assertEqual(render_event({"type": "assistant", "message": {}}), [])

    def test_a_malformed_event_does_not_raise(self):
        self.assertEqual(render_event({}), [])
        self.assertEqual(render_event({"type": "assistant", "message": {"content": "nope"}}), [])
        self.assertEqual(render_event({"type": "user", "message": {"content": [None, 3]}}), [])


if __name__ == "__main__":
    unittest.main()
