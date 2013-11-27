#!/usr/bin/env python
"""
Hold jip jobs

Usage:
    jip-hold [-j <id>...] [-J <cid>...] [--db <db>]
    jip-hold [--help|-h]

Options:
    --db <db>                Select a path to a specific job database
    -j, --job <id>           List jobs with specified id
    -J, --cluster-job <cid>  List jobs with specified cluster id
    -h --help                Show this help message
"""
import jip.db
import jip.jobs
from . import query_jobs_by_ids, read_ids_from_pipe, confirm
from . import parse_args


def main():
    args = parse_args(__doc__, options_first=False)
    ####################################################################
    # Query jobs
    ####################################################################
    job_ids = args["--job"]
    cluster_ids = args["--cluster-job"]

    ####################################################################
    # read job id's from pipe
    ####################################################################
    job_ids = [] if job_ids is None else job_ids
    job_ids += read_ids_from_pipe()

    jip.db.init()
    session = jip.db.create_session()
    jobs = query_jobs_by_ids(session, job_ids=job_ids,
                             cluster_ids=cluster_ids,
                             archived=None, query_all=False)
    jobs = list(jobs)
    if len(jobs) == 0:
        return

    if confirm("Are you sure you want "
               "to hold %d jobs" % len(jobs),
               False):
        import jip
        import jip.db
        count = 0
        for j in jobs:
            if j.state in jip.db.STATES_ACTIVE and \
                    not j.state == jip.db.STATE_HOLD:
                #cancel the job
                j.cancel(remove_logs=True)
                j.state = jip.db.STATE_HOLD
                j.start_date = None
                j.finish_date = None
                # remove logs
                count += 1
                session.add(j)
        session.commit()
        print "%d jobs put on hold" % count


if __name__ == "__main__":
    main()
