# Temporary migration cleanup

`app.py:init_db` contains the block marked `TEMPORARY admin bootstrap migration`.
It adds `allowed_emails.is_admin` and grants admin access to all existing users
once. New users default to non-admin; admins can create projects and manage
the Admin checkboxes on the Users page.

After this release has been deployed to existing databases, remove that
temporary block in a subsequent commit, along with its legacy migration test
and this cleanup instruction. Keep the `is_admin` column in the permanent
schema, its default of 0, and the authorization checks. Do not replace the
bootstrap with an unconditional UPDATE: restarts must preserve revoked access.

The adjacent `TEMPORARY expenditure bootstrap migration` similarly adds
`expenditure_authorization` and grants it to all existing users once. Remove
that block and its legacy migration test after deployment in a later commit;
keep the permanent column with default 0 and all permission checks. Both
bootstrap blocks share the surrounding transaction; retain it until both are
removed. Admins manage expenditure authorization independently of admin status.
