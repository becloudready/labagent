# Acme Corp Backup and Recovery Standard

## Scope

This standard applies to every production system. Each system has an owner who is responsible for its backups and for
proving that they can be restored.

## Backup schedule

Databases are backed up every night, and Tier 1 databases also keep a continuous log so they can be restored to any minute
in the last seven days. File storage is backed up weekly with a nightly incremental. Backups are encrypted and stored in a
different region from the system they protect.

## Retention

Nightly backups are kept for 35 days. Weekly backups are kept for one year. Backups that hold personal data follow the same
retention rules as the data itself: they cannot outlive the 90-day limit for logs with personal data.

## Recovery targets

Tier 1 systems must be back within four hours (recovery time objective, RTO) and may lose at most one hour of data
(recovery point objective, RPO). Tier 2 systems have an RTO of 24 hours and an RPO of 24 hours. The standard is referenced
as BR-3 in audits.

## Restore tests

A restore test is run every quarter for Tier 1 systems and every year for Tier 2 systems. The test restores into an
isolated environment and compares record counts with production. A backup that has never been restored is not counted as a
backup. Failed tests are reported to the system owner and fixed within 30 days.

## Ransomware

Backups must be immutable for at least 14 days so that an attacker with administrator rights cannot delete them.
