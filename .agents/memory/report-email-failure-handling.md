---
name: Report email failure handling
description: Retry transient SMTP setup failures without risking duplicate sends.
---

Use bounded retries while establishing and authenticating the SMTP connection. Do not automatically retry a send operation after `sendmail` begins, because the server may have accepted some recipients before a disconnect. In aggregate report workflows, allow each eligible email step to run, then fail the overall workflow with a summary if any delivery step failed.

**Why:** A connection reset can prevent one message from being sent, while retrying after a partial send can create duplicates and stopping at the first failure suppresses unrelated reports.

**How to apply:** Keep retries before message transmission, continue-on-error on individual report delivery steps, and a final outcome check that leaves actual delivery failures visible.