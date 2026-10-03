"""Checks for the 🔗 Sync Hub: matching, conflict rules, the approval outbox and the hard stop.
Run:  python -m unittest discover tests"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402
import bots  # noqa: E402
import hub  # noqa: E402

LEAD = "Name: Jane Smith\nPhone: (303) 555-0142\nEmail: jane@example.com\nLives in: Littleton, CO\nWants: Kitchen\nOwns the home: Yes\nBudget: $40k+\nWants to start: 1-3 months"


class FakeCRM(hub.Connector):
    """A pretend CRM that keeps its records in memory."""
    id, label = "fake", "Fake CRM"
    fields = ("name", "email", "phone", "stage")

    def __init__(self):
        self.rows, self.writes, self.n = {}, [], 0

    def fetch(self):
        return [{"id": k, "updated_at": v.get("_at"), "fields": {f: v.get(f) for f in self.fields}} for k, v in self.rows.items()]

    def write(self, ext_id, fields):
        self.writes.append((ext_id, dict(fields)))
        if ext_id is None:
            self.n += 1
            ext_id = f"x{self.n}"
            self.rows[ext_id] = {}
        self.rows[ext_id].update(fields)
        return ext_id


class HubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        app.DB_PATH = os.path.join(self.tmp, "test.db")
        app.init_db()
        bots.setup(app.db, app.db_lock)
        hub.CONNECTORS.clear()
        self.fake = FakeCRM()
        hub.CONNECTORS["fake"] = self.fake
        hub.setup(app.db, app.db_lock, hub.LocalConnector(app.all_leads, app.update_lead, app.create_lead_from_hub, app.STAGES))
        for sid in ("remodflow", "fake"):
            hub.set_system(sid, "enabled", True)

    def allow_writes(self, *sids):
        for sid in sids:
            hub.set_system(sid, "can_write", True)

    def test_same_person_in_two_crms_is_one_master_record(self):
        app.add_lead(LEAD)
        self.fake.rows["a1"] = {"name": "Jane Smith", "email": "JANE@example.com ", "phone": "303.555.0142"}
        hub.pull("remodflow")
        hub.pull("fake")
        self.assertEqual(len(hub.records()), 1)
        self.assertEqual({l["system"] for l in hub.records()[0]["links"]}, {"remodflow", "fake"})

    def test_nothing_is_written_until_approved(self):
        app.add_lead(LEAD)
        hub.pull("remodflow")
        [o] = hub.outbox()
        self.assertEqual(o["system"], "fake")
        self.assertEqual(self.fake.writes, [])  # only asked
        self.assertIn("error", hub.decide(o["id"], True))  # writing to this CRM is still switched off
        self.assertEqual(self.fake.writes, [])
        self.allow_writes("fake")
        self.assertEqual(hub.decide(o["id"], True), {"ok": True})
        self.assertEqual(self.fake.writes[0][1]["email"], "jane@example.com")
        # the CRM now matches, so its next pull changes nothing and asks for nothing
        hub.pull("fake")
        self.assertEqual(hub.outbox(), [])
        self.assertEqual(len(hub.records()), 1)

    def test_a_change_in_one_crm_flows_to_the_other(self):
        lead = app.add_lead(LEAD)
        self.allow_writes("fake", "remodflow")
        hub.pull("remodflow")
        hub.decide_all(True)
        ext = next(iter(self.fake.rows))
        self.fake.rows[ext]["stage"] = "Proposal Sent"
        hub.pull("fake")
        [o] = hub.outbox()
        self.assertEqual((o["system"], o["fields"]), ("remodflow", {"stage": "Proposal Sent"}))
        hub.decide(o["id"], True)
        with app.db() as conn:
            self.assertEqual(app.get_lead(conn, lead["id"])["stage"], "Proposal Sent")
        self.assertEqual(hub.outbox(), [])

    def test_new_customer_in_other_crm_is_added_here_after_approval(self):
        self.allow_writes("remodflow")
        self.fake.rows["b7"] = {"name": "Bo Diaz", "email": "bo@example.com", "phone": "3035550100", "stage": "Proposal Sent"}
        hub.pull("fake")
        self.assertEqual(app.all_leads(), [])
        [o] = hub.outbox()
        hub.decide(o["id"], True)
        [lead] = app.all_leads()
        self.assertEqual((lead["name"], lead["email"], lead["stage"]), ("Bo Diaz", "bo@example.com", "Proposal Sent"))
        hub.pull("remodflow")  # reading it back doesn't make a duplicate
        self.assertEqual(len(hub.records()), 1)

    def test_disagreement_follows_the_field_rule(self):
        lead = app.add_lead(LEAD)
        self.allow_writes("fake", "remodflow")
        hub.pull("remodflow")
        hub.decide_all(True)
        ext = next(iter(self.fake.rows))
        hub.set_rule("stage", "ask")
        # both CRMs change the stage before syncing
        app.update_lead(lead["id"], {"stage": "Estimating/Design"})
        self.fake.rows[ext]["stage"] = "Proposal Sent"
        self.fake.rows[ext]["_at"] = "2000-01-01T00:00:00"  # older edit, but the rule is "ask me"
        hub.pull("remodflow")
        hub.pull("fake")
        [c] = hub.conflicts()
        self.assertEqual((c["current"]["v"], c["incoming"]["v"]), ("Estimating/Design", "Proposal Sent"))
        hub.resolve(c["id"], "incoming")
        self.assertEqual(hub.records()[0]["data"]["stage"]["v"], "Proposal Sent")
        self.assertTrue(any(o["system"] == "remodflow" and o["fields"] == {"stage": "Proposal Sent"} for o in hub.outbox()))

    def test_boss_crm_wins(self):
        lead = app.add_lead(LEAD)
        self.allow_writes("fake", "remodflow")
        hub.pull("remodflow")
        hub.decide_all(True)
        ext = next(iter(self.fake.rows))
        hub.set_rule("stage", "remodflow")
        app.update_lead(lead["id"], {"stage": "Estimating/Design"})
        self.fake.rows[ext]["stage"] = "Proposal Sent"
        self.fake.rows[ext]["_at"] = "2999-01-01T00:00:00"  # newer, but Remod Flow is the boss for stage
        hub.pull("remodflow")
        hub.pull("fake")
        self.assertEqual(hub.conflicts(), [])
        self.assertEqual(hub.records()[0]["data"]["stage"]["v"], "Estimating/Design")
        [o] = hub.outbox()  # and the fake CRM is asked to match
        self.assertEqual((o["system"], o["fields"]), ("fake", {"stage": "Estimating/Design"}))

    def test_hard_stop_freezes_syncing(self):
        app.add_lead(LEAD)
        self.allow_writes("fake")
        hub.pull("remodflow")
        o = hub.outbox()[0]
        bots.set_hard_stop(True)
        self.assertEqual(hub.outbox(), [])  # waiting writes are cancelled
        self.assertIn("error", hub.decide(o["id"], True))
        self.assertIn("error", hub.pull("fake"))
        self.assertEqual(self.fake.writes, [])
        bots.set_hard_stop(False)
        hub.pull("remodflow")
        self.assertEqual(len(hub.outbox()), 1)

    def test_request_made_before_a_match_writes_to_the_matched_record(self):
        app.add_lead(LEAD)
        self.allow_writes("fake")
        hub.pull("remodflow")  # asks to create Jane in the fake CRM
        self.fake.rows["a1"] = {"name": "Jane Smith", "email": "jane@example.com"}
        hub.pull("fake")  # ...but she's there already and gets matched
        [o] = hub.outbox()
        hub.decide(o["id"], True)
        self.assertEqual(list(self.fake.rows), ["a1"])  # updated, not duplicated
        self.assertEqual(self.fake.writes[0][0], "a1")

    def test_first_match_that_disagrees_asks_you(self):
        app.add_lead(LEAD)
        self.fake.rows["a1"] = {"name": "Jane Smith", "email": "jane@example.com", "stage": "Closed-Won"}
        hub.pull("remodflow")
        hub.pull("fake")
        [c] = hub.conflicts()
        self.assertEqual((c["field"], c["incoming"]["v"]), ("stage", "Closed-Won"))
        self.assertFalse(any("stage" in o["fields"] for o in hub.outbox()))  # nothing is sent until you pick
        hub.resolve(c["id"], "incoming")
        self.assertTrue(any(o["system"] == "remodflow" and o["fields"].get("stage") == "Closed-Won" for o in hub.outbox()))

    def test_saying_no_is_remembered(self):
        app.add_lead(LEAD)
        hub.pull("remodflow")
        hub.decide(hub.outbox()[0]["id"], False)
        hub.pull("remodflow")
        self.assertEqual(hub.outbox(), [])  # not asked again for the same thing
        self.assertEqual(self.fake.writes, [])

    def test_turning_writing_off_cancels_its_waiting_changes(self):
        app.add_lead(LEAD)
        self.allow_writes("fake")
        hub.pull("remodflow")
        hub.set_system("fake", "can_write", False)
        self.assertEqual(hub.outbox(), [])

    def test_keys_never_reach_the_log_or_the_screen(self):
        hub.CONNECTORS["ghl"] = hub.GoHighLevelConnector()
        hub.set_secrets("ghl", {"token": "pit-SECRET123", "location_id": "loc1"})
        self.assertEqual(hub.secret("ghl", "token"), "pit-SECRET123")
        self.assertNotIn("SECRET123", json.dumps(bots.read_log()))
        self.assertNotIn("SECRET123", json.dumps(hub.overview()))

    def test_every_master_change_is_versioned(self):
        lead = app.add_lead(LEAD)
        hub.pull("remodflow")
        app.update_lead(lead["id"], {"phone": "720-555-0199"})
        hub.pull("remodflow")
        rid = hub.records()[0]["id"]
        self.assertEqual([h["version"] for h in hub.record_history(rid)], [2, 1])


class TimeTest(unittest.TestCase):
    def test_utc_times_become_local(self):
        from datetime import datetime, timezone
        utc = datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(hub.local_time("2026-10-03T18:00:00.000Z"), utc.astimezone().replace(tzinfo=None).isoformat(timespec="seconds"))
        self.assertIsNone(hub.local_time(None))


class GoHighLevelTest(unittest.TestCase):
    """Checks how GoHighLevel's answers are turned into hub fields, without going on the internet."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        app.DB_PATH = os.path.join(self.tmp, "test.db")
        app.init_db()
        bots.setup(app.db, app.db_lock)
        hub.setup(app.db, app.db_lock, hub.LocalConnector(app.all_leads, app.update_lead, app.create_lead_from_hub, app.STAGES))
        self.ghl = hub.GoHighLevelConnector()
        hub.set_secrets("ghl", {"token": "t", "location_id": "loc1", "pipeline_id": "p1"})

    def fake_call(self, method, path, body=None, query=None):
        self.calls.append((method, path, body, query))
        if path == "/opportunities/pipelines":
            return {"pipelines": [{"id": "p1", "stages": [{"id": "s1", "name": "New Lead"}, {"id": "s2", "name": "Proposal Sent"}]}]}
        if path == "/opportunities/search":
            if query and query.get("contact_id"):
                return {"opportunities": [{"id": "o9"}]} if query["contact_id"] == "c1" else {"opportunities": []}
            return {"opportunities": [{"contactId": "c1", "pipelineStageId": "s2", "monetaryValue": 45000.0}]}
        if path == "/contacts/":
            return {"contacts": [{"id": "c1", "firstName": "jane", "lastName": "smith", "contactName": "jane smith",
                                  "email": "jane@example.com", "phone": "+13035550142", "city": "Littleton",
                                  "dateUpdated": "2026-10-01T10:00:00.000Z"}], "meta": {}}
        if path == "/contacts/upsert":
            return {"contact": {"id": "c2"}}
        return {}

    def test_reads_contacts_with_stage_and_worth(self):
        self.calls = []
        with mock.patch.object(self.ghl, "_call", self.fake_call):
            [c] = self.ghl.fetch()
        self.assertEqual(c["id"], "c1")
        self.assertEqual(c["fields"], {"name": "Jane Smith", "email": "jane@example.com", "phone": "+13035550142",
                                       "location": "Littleton", "stage": "Proposal Sent", "job_value": 45000})

    def test_writes_contact_and_moves_its_opportunity(self):
        self.calls = []
        with mock.patch.object(self.ghl, "_call", self.fake_call):
            self.assertEqual(self.ghl.write("c1", {"name": "Jane Q Smith", "stage": "New Lead"}), "c1")
            self.assertEqual(self.ghl.write(None, {"name": "Bo Diaz", "stage": "Proposal Sent"}), "c2")
        self.assertIn(("PUT", "/contacts/c1", {"firstName": "Jane", "lastName": "Q Smith"}, None), self.calls)
        self.assertIn(("PUT", "/opportunities/o9", {"pipelineStageId": "s1"}, None), self.calls)
        new_opp = [c for c in self.calls if c[:2] == ("POST", "/opportunities/")][0][2]
        self.assertEqual((new_opp["contactId"], new_opp["pipelineStageId"]), ("c2", "s2"))

    def test_unknown_stage_is_refused(self):
        self.calls = []
        with mock.patch.object(self.ghl, "_call", self.fake_call), self.assertRaises(RuntimeError):
            self.ghl.write("c1", {"stage": "Closed-Won"})


if __name__ == "__main__":
    unittest.main()
