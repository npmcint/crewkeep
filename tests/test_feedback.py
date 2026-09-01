"""Feedback module: submit / review lifecycle (jobhunt pattern)."""

import pytest

import feedback


def test_submit_and_get():
    fid = feedback.submit("adrian", "feature", "Add a night-shift roster view")
    item = feedback.get_item(fid)
    assert item["user_id"] == "adrian"
    assert item["category"] == "feature"
    assert item["status"] == "new"
    assert item["admin_note"] == ""


def test_submit_validation():
    with pytest.raises(ValueError):
        feedback.submit("", "feature", "x")          # no user
    with pytest.raises(ValueError):
        feedback.submit("adrian", "spam", "x")       # bad category
    with pytest.raises(ValueError):
        feedback.submit("adrian", "feature", "")     # empty message
    with pytest.raises(ValueError):
        feedback.submit("adrian", "bug", "x" * 2001)  # too long


def test_mine_only_own():
    feedback.submit("adrian", "bug", "Screen buttons dead on mobile")
    feedback.submit("nigel", "feature", "Export the pool list")
    mine = feedback.mine("adrian")
    assert len(mine) == 1
    assert mine[0]["message"].startswith("Screen buttons")
    assert feedback.mine("nobody") == []


def test_set_status_lifecycle():
    fid = feedback.submit("adrian", "feature", "Payslips in the app")
    item = feedback.set_status(fid, "approved", admin_note="good idea")
    assert item["status"] == "approved"
    assert item["admin_note"] == "good idea"
    item = feedback.set_status(fid, "done")
    assert item["status"] == "done"
    with pytest.raises(ValueError):
        feedback.set_status(fid, "maybe")            # bad status
    with pytest.raises(ValueError):
        feedback.set_status(99999, "approved")       # not found


def test_list_and_counts():
    feedback.submit("adrian", "bug", "One")
    feedback.submit("adrian", "feature", "Two")
    fid = feedback.submit("nigel", "other", "Three")
    feedback.set_status(fid, "rejected")
    assert len(feedback.list_items()) == 3
    assert len(feedback.list_items(status="new")) == 2
    assert len(feedback.list_items(status="rejected")) == 1
    assert feedback.counts() == {"new": 2, "rejected": 1}
