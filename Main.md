IdleMerge auomatically merges commits from one Subversion branch to an other.

# Introduction #

The target use case is a 3 branches model which works well for 'online' projects and similar to
the Debian branching model of 'stable', 'testing', 'unstable'. The idea is that any changes made
to 'stable' should always go to 'testing' and then 'unstable'. The traditional way is to always
work in 'unstable' (usually trunk) and then merge up to the branches with cherry pick frequently
and branch cut on a regular basis. Here we call the branches trunk, stable, prod, and with a two
weeks release cycle:
  * trunk: get all the medium term work 1-2 weeks from release. This is the lower branch.
  * stable: get the work for release within a week
  * prod: currently live code or soon to be live, holds the patch releases code.

# Details #

The issues with that are:
  * making a simple bug fix internted for stable in unstable is difficult, because of how unstable it is. The more radical work than happens in unstable make it challenging to find a good time when the rest of the code is in a testable/runnable state.
  * additional work to cherry pick the fixes to put in the stable/prod branches, sometimes two cherry-picks are required.
  * unreliable tracking, some engineers will cherry pick 'manually' bypassing the native svn merge command and inlude a last minute fix in the stable/testing branches. When the code get released these last minute changes are lost because they never made it back to the trunk.
  * ease of use. Artists and other non engineers usually do not know how to merge. They just need to save in a different directory when appropriate, the auomerge takes care of the rest.

Downsides are:
  * merge conflicts are public. Merge conflicts will happen. In practice they are not frequent if the workflow is followed. Subversion is not very good at resolving obvious non-conflict, some of it can be automated safely.
  * merge conflicts block the automergeing. If an important fix is pending in the merge queue because a conflict is pending resolution then engineers should not wait for the queue to clear up automatically but should be proactive to either fix the conflict, or merge down the critical fixes.
  * somtimes some fixes are really for the prod branch only, use the NO\_MERGE flag as part of the commit. To be used sparringly otherwise it makes the workflow unreliable.