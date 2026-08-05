# Review queue

Small issues found in passing, left for a human pass rather than fixed inline. Each item
names the file, the problem, and the decision to be made — not a patch to rubber-stamp.

Cleared items move to the bottom with their resolution.

---

## Open

### 1. Agent volunteers the remaining verification-attempt count
**Where:** `src/extend_flow.py`, `commit_extension`, the `VERIFICATION_FAILED` branch.

The customer-facing message says `Attempt N of 3`, and the model relays it ("you still have
two more tries"). Friendlier, but it also tells someone probing a reservation ID exactly how
much runway they have before the session escalates.

**The call:** keep it (transparency, and the reservation ID is the weaker secret anyway) or
drop the count and let escalation arrive unannounced. Either is defensible; it's a
security-vs-UX judgment, which is why it's here rather than silently decided.

---

## Cleared

_(none yet)_
