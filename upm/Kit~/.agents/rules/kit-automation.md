---
trigger: always_on
---

# Post-Edit Automation (rendered from kit canon)

After creating, editing, moving, or deleting project files in this task, run:

```
python3 .agents/scripts/check_unity_meta.py
```

Fix every reported issue before finishing the task. Do not skip this check.
