#!/usr/bin/env python
"""
Submit a jip script to a remote cluster

usage: jip-submit [-f] [-k] [-P <profile>] [-t <time>] [-q <queue>]
                  [-p <prio>] [-A <account>] [-C <cpus>] [-m <mem>] [-n <name>]
                  [-d <db>] [-H] [--dry] <file> [<args>...]

Options:
  -f, --force              force command execution
  -k, --keep               do not perform a cleanup step after job failure or
                           cancellation
  -P, --profile <profile>  Select a job profile for resubmission
  -t, --time <time>        Max wallclock time for the job
  -q, --queue <queue>      Job queue
  -p, --priority <prio>    Job priority
  -A, --account <account>  The account to use for submission
  -C, --cpus <cpus>        Number of CPU's assigned to the job
  -m, --max-mem <mem>      Max memory assigned to the job
  -n, --name <name>        Job name
  -d, --db <db>            Path to the database that will be used to store the
                           job information
  -H, --hold               submit job put put in on hold and don't send
                           it to the queue
  --dry                    Do not submit but show the dry configuration
  <file>                   the script that will be executed
  <args>                   optional script argument

Other Options:
    -h --help             Show this help message

"""
import sys

from . import parse_args
from jip.model import Script, ScriptError, ValidationException
from jip.executils import load_job_profile, create_jobs, submit


def main(argv=None):
    args = parse_args(__doc__, argv=argv)
    script_file = args["<file>"]
    script_args = args["<args>"]
    # parse the script
    script = Script.from_file(script_file)
    script.parse_args(script_args)
    # always catch help message
    if "-h" in script_args or "--help" in script_args:
        print script.help()
        sys.exit(0)

    try:
        submit_script(script, args, dry=args['--dry'])
    except ValidationException, va:
        sys.stderr.write(str(va))
        sys.stderr.write("\n")
        sys.exit(1)
    except ScriptError, sa:
        sys.stderr.write(str(sa))
        sys.stderr.write("\n")
        sys.exit(1)


def submit_script(script, jip_args, dry=False):
    from jip.db import create_session, init
    ## initialize custom database location
    if dry:
        init(in_memory=True)
    else:
        db_path = jip_args.get('--db', None)
        if db_path is not None:
            init(path=db_path)

    session = create_session()
    ## create jobs
    jobs = create_jobs(script, keep=jip_args["--keep"], session=session)
    # laod default profile
    profile = load_job_profile(profile_name=jip_args.get("--profile",
                                                         None),
                               time=jip_args["--time"],
                               queue=jip_args["--queue"],
                               priority=jip_args["--priority"],
                               account=jip_args["--account"],
                               cpus=jip_args["--cpus"],
                               max_mem=jip_args["--max-mem"],
                               name=jip_args["--name"],
                               load_default=True
                               )
    if dry:
        from jip_run import show_dry_run
        for job in jobs:
            job.update_profile(profile)
        show_dry_run(jobs)
        return

    if jip_args["--hold"]:
        from jip.executils import set_state
        from jip.db import STATE_HOLD
        # save jobs, but don't submit
        for job in jobs:
            job.update_profile(profile)
            set_state(STATE_HOLD, job, session=session)
            print "Job %d submitted and on hold" % (job.id)
        session.commit()
        return
    try:
        submitted, skipped = submit(jobs, profile, force=jip_args["--force"],
                                    session=session)
        map(session.delete, skipped)
        session.commit()
        session.close()
    except Exception:
        ## delete all job, couldn't submit
        map(session.delete, jobs)
        session.commit()
        session.close()
        raise
    for job in submitted:
        print "Job %d with remote id %s submitted" % (job.id, job.job_id)
    for job in skipped:
        print "Job %d skipped, output exists" % (job.id)


if __name__ == "__main__":
    main()