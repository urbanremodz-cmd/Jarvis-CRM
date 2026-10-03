"""Checks for the Flow Map: the code scan and every bot safety rule.  Run:  python -m unittest discover tests"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402
import bots  # noqa: E402
import flowmap  # noqa: E402

LEAD = "Name: Jane Smith\nPhone: (303) 555-0142\nLives in: Littleton, CO\nWants: Kitchen\nOwns the home: Yes\nBudget: $40k+\nWants to start: 1-3 months"


class ScanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.map = flowmap.scan()
        cls.pages = {p["id"]: p for p in cls.map["pages"]}

    def test_every_menu_page_is_mapped(self):
        for pid in ("pipeline", "add", "follow", "videos", "guide", "howto", "flowmap", "server"):
            self.assertIn(pid, self.pages)
        self.assertEqual(self.map["warnings"], [])

    def test_reads_saves_and_feeds(self):
        p = self.pages
        self.assertIn("table:leads", p["add"]["saves"])
        self.assertIn("table:leads", p["follow"]["reads"])
        self.assertIn("setting:layout", p["pipeline"]["saves"])
        self.assertIn("setting:layout", p["guide"]["reads"])
        self.assertIn("guide", p["pipeline"]["feeds"])
        self.assertIn("pipeline", p["add"]["feeds"])
        self.assertEqual(p["howto"]["saves"], [])
        self.assertIn("file:videos/videos.json", p["videos"]["saves"])

    def test_schedules_and_outside_services(self):
        p = self.pages
        self.assertTrue(any("Monthly emails" in s["label"] for s in p["follow"]["schedules"]))
        self.assertTrue(any("Check-in" in s["label"] for s in p["follow"]["schedules"]))
        self.assertTrue(any("watcher" in s["label"] for s in p["server"]["schedules"]))
        names = lambda pid: {o["name"] for o in p[pid]["outside"]}
        self.assertIn("Windows (opens a folder)", names("videos"))
        self.assertIn("Printer", names("guide"))
        self.assertIn("GoHighLevel", names("howto"))
        self.assertTrue(all(o["kind"] == "mentioned" for o in p["howto"]["outside"]))


class BotSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        app.DB_PATH = os.path.join(self.tmp, "test.db")
        app.init_db()
        bots.setup(app.db, app.db_lock)

    def make_flow(self, approve=True, allow=True):
        f = bots.save_flow({"name": "Flag big kitchens", "bot": "pipeline", "trigger": {"type": "lead_added"},
                            "conditions": [{"field": "project", "op": "is", "value": "Kitchen"}],
                            "actions": [{"type": "move", "stage": "Discovery Call Scheduled"}]})
        if allow:
            for perm in ("watch:table:leads", "change:table:leads"):
                bots.set_perm("pipeline", perm, True, "My Leads")
        if approve:
            bots.set_flow_state(f["id"], "approve")
        return bots.flow(f["id"])

    def pending(self):
        return bots.runs("pending")

    def test_new_flow_does_nothing_until_approved(self):
        f = self.make_flow(approve=False)
        self.assertEqual(f["status"], "needs approval")
        app.add_lead(LEAD)
        self.assertEqual(self.pending(), [])

    def test_permissions_start_off_and_block(self):
        self.assertEqual(bots.perms("pipeline"), {})
        self.make_flow(allow=False)
        app.add_lead(LEAD)
        self.assertEqual(self.pending(), [])
        self.assertTrue(any(l["action"] == "blocked" for l in bots.read_log()))

    def test_bot_only_asks_and_approval_makes_the_change(self):
        self.make_flow()
        lead = app.add_lead(LEAD)
        self.assertEqual(lead["stage"], "Pre-Qualified")
        [run] = self.pending()
        with app.db() as conn:
            self.assertEqual(app.get_lead(conn, lead["id"])["stage"], "Pre-Qualified")  # nothing changed yet
        self.assertEqual(bots.decide(run["id"], True, app.run_bot_action), {"ok": True})
        with app.db() as conn:
            self.assertEqual(app.get_lead(conn, lead["id"])["stage"], "Discovery Call Scheduled")
        self.assertEqual(self.pending(), [])  # the bot's own move doesn't start more flows

    def test_saying_no_changes_nothing(self):
        self.make_flow()
        lead = app.add_lead(LEAD)
        bots.decide(self.pending()[0]["id"], False, app.run_bot_action)
        with app.db() as conn:
            self.assertEqual(app.get_lead(conn, lead["id"])["stage"], "Pre-Qualified")

    def test_hard_stop_overrides_everything(self):
        self.make_flow()
        app.add_lead(LEAD)
        run = self.pending()[0]
        bots.set_hard_stop(True)
        self.assertEqual(self.pending(), [])  # waiting requests are cancelled
        self.assertIn("error", bots.decide(run["id"], True, app.run_bot_action))
        app.add_lead(LEAD.replace("Jane", "Ann"))
        self.assertEqual(self.pending(), [])  # nothing new is noticed
        bots.tick(app.all_leads)
        self.assertEqual(self.pending(), [])
        bots.set_hard_stop(False)
        app.add_lead(LEAD.replace("Jane", "Bea"))
        self.assertEqual(len(self.pending()), 1)

    def test_editing_a_flow_needs_new_approval(self):
        f = self.make_flow()
        app.add_lead(LEAD)
        run = self.pending()[0]
        bots.save_flow({**f, "name": "Changed"})
        self.assertEqual(bots.flow(f["id"])["status"], "needs approval")
        self.assertIn("error", bots.decide(run["id"], True, app.run_bot_action))

    def test_every_change_is_versioned_and_logged(self):
        bots.set_perm("pipeline", "watch:table:leads", True, "My Leads")
        bots.set_perm("pipeline", "watch:table:leads", False, "My Leads")
        hist = bots.history("perms", "pipeline")
        self.assertEqual([h["version"] for h in hist], [2, 1])
        bots.restore("perms", "pipeline", 1)
        self.assertEqual(bots.history("perms", "pipeline")[0]["version"], 3)
        self.assertTrue(bots.perms("pipeline")["watch:table:leads"])
        self.assertGreaterEqual(len(bots.read_log()), 3)

    def test_going_back_cannot_switch_bots_on(self):
        bots.set_hard_stop(True)
        bots.set_hard_stop(False)
        bots.set_hard_stop(True)
        self.assertIn("error", bots.restore("hardstop", "all", 2))
        self.assertTrue(bots.hard_stopped())
        f = self.make_flow()
        bots.set_flow_state(f["id"], "pause")
        self.assertIn("error", bots.restore("flow_state", f["id"], 2))
        self.assertEqual(bots.flow(f["id"])["status"], "paused")

    def test_approve_refuses_a_version_you_did_not_see(self):
        f = self.make_flow(approve=False)
        bots.save_flow({**f, "name": "Changed"})
        self.assertIn("error", bots.set_flow_state(f["id"], "approve", f["version"]))
        self.assertEqual(bots.flow(f["id"])["status"], "needs approval")

    def test_time_based_trigger(self):
        f = bots.save_flow({"name": "Daily note", "bot": "pipeline", "trigger": {"type": "daily", "time": "00:00"},
                            "conditions": [], "actions": [{"type": "note", "text": "check in"}]})
        for perm in ("watch:table:leads", "change:table:leads"):
            bots.set_perm("pipeline", perm, True, "My Leads")
        bots.set_flow_state(f["id"], "approve")
        lead = app.add_lead(LEAD)
        bots.tick(app.all_leads)
        bots.tick(app.all_leads)  # once per day per customer
        [run] = self.pending()
        bots.decide(run["id"], True, app.run_bot_action)
        with app.db() as conn:
            self.assertIn("🤖 check in", app.get_lead(conn, lead["id"])["notes"])


if __name__ == "__main__":
    unittest.main()
