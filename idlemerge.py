#!/usr/bin/env python2.6
# Style based on: http://google-styleguide.googlecode.com/svn/trunk/pyguide.html
# Exception: 100 characters width.
#
# Copyright idle-games.com
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import datetime
import os
import re
import select
import shutil
import subprocess
import sys
import xml.etree.ElementTree
from optparse import OptionParser


USAGE = """%prog <options

IdleMerge auomatically merges commits from one Subversion branch to an other.

The target use case is a 3 branches model which works well for 'online' projects and similar to
the Debian branching model of 'stable', 'testing', 'unstable'. The idea is that any changes made
to 'stable' should always go to 'testing' and then 'unstable'. The traditional way is to always
work in 'unstable' (usually trunk) and then merge up to the branches with cherry pick frequently
and branch cut on a regular basis. Here we call the branches trunk, stable, prod, and with a two
weeks release cycle:
 - trunk: get all the medium term work 1-2 weeks from release. This is the lower branch.
 - stable: get the work for release within a week
 - prod: currently live code or soon to be live, holds the patch releases code.

The issues with that are:
 - making a simple bug fix internted for stable in unstable is difficult, because of how unstable
   it is. The more radical work than happens in unstable make it challenging to find a good time
   when the rest of the code is in a testable/runnable state.
 - additional work to cherry pick the fixes to put in the stable/prod branches, sometimes two
   cherry-picks are required.
 - unreliable tracking, some engineers will cherry pick 'manually' bypassing the native svn merge
   command and inlude a last minute fix in the stable/testing branches. When the code get released
   these last minute changes are lost because they never made it back to the trunk.
 - ease of use. Artists and other non engineers usually do not know how to merge. They just need to
   save in a different directory when appropriate, the auomerge takes care of the rest.

Downsides are:
 - merge conflicts are public. Merge conflicts will happen. In practice they are not frequent if
   the workflow is followed. Subversion is not very good at resolving obvious non-conflict, some
   of it can be automated safely.
 - merge conflicts block the automergeing. If an important fix is pending in the merge queue
   because a conflict is pending resolution then engineers should not wait for the queue to clear
   up automatically but should be proactive to either fix the conflict, or merge down the critical
   fixes.
 - somtimes some fixes are really for the prod branch only, use the NO_MERGE flag as part of the
   commit. To be used sparringly otherwise it makes the workflow unreliable.

>"""

DEFAULT_NO_MERGE_PATTERNS = (
    'maven-release-plugin', 'NOMERGE', 'NO-MERGE', 'NO MERGE', 'NO_MERGE')

# Sample merge logs:
# [automerge ^/branches/prod@1234] Original comment for the revision
#   on multiple lines
# -- IDLEMERGE DATA --
#   REVISIONS=1234
#   MERGEINFO_REVISIONS=1230,1233
# 
# merge revisions r1235,1236 from ^/x to ^/bar
#   -- IDLEMERGE DATA --
#   REVISIONS=1235,1236
#   MERGEINFO_REVISIONS=1230,1233
#   r39389 | _jenkins | 2012-02-17 17:13:35 -0800 (Fri, 17 Feb 2012)
#     Original comment for 1235
#     spanning multiple lines
#   r39389 | _jenkins | 2012-02-17 17:13:35 -0800 (Fri, 17 Feb 2012)
#     Original comment for 1236
#     spanning multiple lines


class Error(Exception):
    pass

class Conflict(Error):

    def __init__(
        self, revision, mergeinfos=None, merges=None, message=None, source=None, target=None):
        super(Conflict, self).__init__()
        self.revision = revision
        self.mergeinfos = mergeinfos
        self.merges = merges
        self.source = source
        self.target = target
        self._message = message

    def __str__(self):
        message_lines = [self._message] if self._message else []
        message_lines.append('MANUAL MERGE NEEDS TO BE DONE: revision %s by %s from %s' % (
            self.revision, self.revision.author, self.source))
        if self.mergeinfos:
            message_lines.append(
                'Pending record-only merges: ' + revisions_as_string(self.mergeinfos))
        if self.merges:
            message_lines.append('Pending clean merges: ' + revisions_as_string(self.merges))
        return '\n'.join(message_lines)


def parse_args(argv):
    parser = OptionParser(USAGE)

    parser.add_option('-S', '--source', dest='source',
        help='source repository url to merge [REQUIRED]')
    parser.add_option('-n', '--noop', dest='noop', action='store_true',
        help='No Operation, do not commit merges')
    parser.add_option('-s', '--single', dest='single', action='store_true',
        help='Merge one revision by one. One two source revisions, two commits')
    parser.add_option('-c', '--concise', dest='concise', action='store_true',
        help='if --single is activated, bundle up mergeinfo only merges together to reduce noise.')
    parser.add_option('-a', '--patterns', dest='patterns',
        help='patterns contained in comments of revisions not to be merged, comma separated')
    parser.add_option('-m', '--max', dest='max', default=10, type='int',
        help='maximum number of revisions to merge in this pass.'
        ' Used for troubleshooting, 0 is infinite.')
    parser.add_option('-r', '--record_only_file', dest='record_only_filename',
        help='file to store/read record-only revisions.')
    parser.add_option('-v', '--verbose', dest='verbose', action='store_true', help='verbose mode')
    # parser.add_option('-V', '--validation', dest='validation', help='validation script')

    # TODO(stephane): options to be implemented:
    # merge subdirs independently as long as no pending conflict is in the same directory.
    #    potentially dangerous
    # ignore revisions: sometime some conflicts cannot be resolved fast enough and are blocking
    #   the merge queue, in suh case it can be valid to 'skip' them temporarily.
    # HEAD, we want to gatekeep the merges with a valid parent build, if it then picks the latest
    #   head when the merge start, it defeats the pupose of gatekeeping.
    # Store mergeingo/revcord-only revisions to disk for the next run. This might shave some
    #   seconds for the next run.
    # Authentication with username and password -- low priority.
    # Validation script: external command to run to resolve remaining conflicts for example.

    options, _ = parser.parse_args(argv[1:])

    if not options.source:
        print USAGE
        raise Error()
    return options


def execute_command(
    command, discard_output=False, verbose=False, stdout=None, stderr=None, password=None):
    """Call a subprocess and handle the stder/stdout.

    Args:
        command: A list fo strings, the command to run.
        discard_output: A boolean, if True do not keep stdout/stderr. Default is False
        verbose: A boolean, if True print the command wit outputs as it runs. Default is False.
        stdout: A file like instance, where to pass stdout of the command only when verbose=True.
            Default is sys.stdout.
        stderr: A file like instance, where to pass stderrof the command only when verbose=True.
            Default is sys.stderr.
        password: A string, a password to replace in the command arguments with the %%PASSWORD%%
            pattern. This allows us to hide the password from the verbose output.
    """
    if stdout is None:
        stdout = sys.stdout
    if stderr is None:
        stderr = sys.stderr

    if verbose:
        print >> stdout, '[DEBUG] executing command %r.' % ' '.join(command)

    if password is not None:
        cmd = [(x if x != '%%PASSWORD%%' else password) for x in command]
    else:
        cmd = command

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    stdout_lines = []
    stderr_lines = []
    out_buffer = process.stdout
    err_buffer = process.stderr
    output_targets = {out_buffer: stdout, err_buffer: stderr}
    lines_targets = {out_buffer: stdout_lines, err_buffer: stderr_lines}
    read = 0
    inputs = (out_buffer, err_buffer)
    while True:
        readable, _ , _ = select.select(inputs, (), ())
        if not readable:
            break
        for stream in readable:
            output = stream.readline()
            if not output:
                continue
            read += 1
            if verbose:
                output_targets[stream].write(output)
            if not discard_output:
                lines_targets[stream].append(output)
        if process.poll() is not None and read == 0:
            break
        read = 0
    return_code = process.wait()

    if verbose:
        print >> stdout, '[DEBUG] exit value : %d' % return_code

    process_output = {
        'return_code': return_code,
        'stdout': stdout_lines,
        'stderr': stderr_lines
    }
    return process_output


class AuthToken(object):

    def __init__(self, username, password):
        self.username = username
        self.password = password


# <path
#    kind="file"
#    action="D|M|A">/trunk/bi/reducer_uid_session.py</path>
class LogPath(object):
    """Abstraction class for <path> entries from svn log -v."""

    def __init__(self, xml_element):
        self._xml = xml_element
        self._action = None

    @property
    def action(self):
        if self._action is None:
            self._action = self._xml.attrib['action']
        return self._action

    @property
    def path(self):
        return self._xml.text

    @property
    def kind(self):
        return self._xml.attrib['kind']

    @property
    def is_file(self):
        return self.kind == 'file'

    @property
    def is_dir(self):
        return not self.is_file


class Revision(object):
    """Svn revision class.

    Sample xml log entry:
        <logentry revision="36317">
            <author>ravi</author>
            <date>2012-01-27T02:08:20.565277Z</date>
            <msg>change of uge test</msg>
        </logentry>

    Args:
        number: An integer or string, the revision number. Optional.
        svn: An SvnWrapper instance. Optional.
        xml_element: An xml.etree.ElementTree instance.
        branch: A string, the path to the branch. Default is ^/ but can be 50% slower.
    """
    def __init__(self, number=None, svn=None, xml_element=None, branch='^/'):
        if number is None and xml_element is None:
            raise Error('Must provide either number or xml to Revision().')
        if svn is None:
            svn = SvnWrapper()
        self.svn = svn
        self.branch = branch
        self._number = int(number) if number is not None else None

        self._xml = xml_element
        self._author = None
        self._date = None
        self._msg = None
        self._full_msg = None
        self._idle_data = None
        self._paths = None

    def __str__(self):
        return str(self.number)

    def __cmp__(self, other):
        return cmp(self.number, other.number)

    @property
    def number(self):
        if self._number is None and self._xml:
            self._number = int(self._xml.attrib['revision'])
        return self._number

    @number.setter
    def number(self, revision_number):
        if int(revision_number) != self.number:
            self._delete_properties()
            self._number = int(revision_number)

    @property
    def xml_element(self):
        if self._xml is None:
            self._get_log()
        return self._xml

    @xml_element.setter
    def xml_element(self, data):
        self._delete_properties()
        self._number = None
        self._xml = data

    @property
    def author(self):
        if self._author is None and self.xml_element:
            self._author = self.xml_element.find('author').text
        return self._author

    @property
    def date(self):
        if self._date is None and self.xml_element:
            date_string = self.xml_element.find('date').text
            self._date = datetime.datetime.strptime(date_string, '%Y-%m-%dT%H:%M:%S.%fZ')
        return self._date

    @property
    def full_msg(self):
        if self._full_msg is None and self.xml_element:
            self._full_msg = self.xml_element.find('msg').text
        return self._msg

    @property
    def msg(self):
        if self._full_msg is None:
            self._get_msg()
        return self._msg

    @property
    def idle_data(self):
        if self._full_msg is None:
            self._get_msg()
        return self._idle_data

    def _get_msg(self):
        if not self.xml_element:
            raise Error('Cannot get data')
        full_msg = self.xml_element.find('msg').text
        self._full_msg = full_msg

        # Note: Python2.7 supports flags=re.MULTILINE -- stephane
        match = re.split(r'(^|\n)-- IDLEMERGE DATA --\n', full_msg, 1)
        self._msg = match[0]
        self._idle_data = match[1] if len(match) > 1 else ''

    @property
    def paths(self):
        if self._paths is None:
            self._paths = [LogPath(x) for x in self.xml_element.find('paths')]
        return self._paths

    def _delete_properties(self):
        self._xml = None
        self._author = None
        self._date = None
        self._msg = None

    def _get_log(self):
        self._delete_properties()
        self.svn.log(['--xml', '-v', '-r', str(self.number), self.branch])
        log = xml.etree.ElementTree.fromstring(''.join(self.svn.stdout))
        self._xml = log.find('logentry')


def revisions_as_string(revisions, separator=', '):
    return separator.join([str(revision) for revision in revisions])


class StatusEntry(object):
    """Wrapper class for svn status entries."""

    def __init__(self, xml_element):
        self._xml = xml_element

        self._wc_status = None
        self._commit = None
        self._commit_fetched = False

    @property
    def path(self):
        return self._xml.attrib['path']

    @property
    def wc_status(self):
        if self._wc_status is None:
            self._wc_status = self._xml.find('wc-status')   # should always return something.
        return self._wc_status

    @property
    def commit(self):
        if self._commit_fetched is False:
            self._commit_fetched = True
            self._commit = self._xml.find('commit')
        return self._commit

    @property
    def props(self):
        """Returns the status for the file/directory properties.

        Returns:
            A string, values are 'none', 'conflicted', 'normal', 'modified'.
        """
        return self.wc_status.attrib['props']

    @property
    def item(self):
        """Returns the status for the file/directory itself.

        Returns:
            A string, values are 'added', 'conflicted', 'deleted', 'normal', 'missing',
            'modified', 'unversionned'.
        """
        return self.wc_status.attrib['item']

    @property
    def wc_revision(self):
        return self.wc_status.attrib.get('revision')

    @property
    def commit_revision(self):
        _commit = self.commit
        if not _commit:
            return None
        return _commit.attrib['revision']

    def is_dir(self):
        return os.path.isdir(self.path)

    def conflict_prej_filepath(self):
        if self.is_dir():
            if self.props == 'conflicted':
                conflict_file = os.path.join(self.path, 'dir_conflicts.prej')
                return conflict_file if os.path.exists(conflict_file) else None
            return None

    @property
    def tree_conflicted(self):
        return self.wc_status.attrib.get('tree-conflicted') == 'true'

    @property
    def has_conflict(self):
        return self.tree_conflicted or 'conflicted' in (self.props, self.item)

    @property
    def has_non_props_changes(self):
        # TODO(stephane): this is a potential 'bug' we should specifically ignore svn:mergeinfo.
        # The downside is small enough to not fix it on the first revision.
        return self.has_conflict or self.item not in ('normal', 'unversionnned')

    @property
    def is_unversionned(self):
        return self.item == 'unversionned'


class Status(object):
    """Wrapper class for 'svn status --xml' results."""

    def __init__(self, xml_element):
        self._xml = xml_element
        self._entries = None
        self._entries_by_path = None
        self._conflict_entries = None
        self._conflict_entries_by_path = None
        self._unversionned = None

    @property
    def entries(self):
        if self._entries is None:
            self._get_entries()
        return self._entries

    @property
    def entries_by_path(self):
        if self._entries_by_path is None:
            self._entries_by_path()
        return self._entries_by_path

    def _get_entries(self):
        _entries_by_path = {}
        _entries = []
        targets = self._xml.findall('target')
        for target in targets:
            entries = [StatusEntry(x) for x in target.findall('entry')]
            for entry in entries:
                if entry.path in _entries_by_path:
                    continue
                _entries.append(entry)
                _entries_by_path[entry.path] = entry
        self._entries = _entries
        self._entries_by_path = _entries_by_path

    @property
    def conflict_entries(self):
        if self._conflict_entries is None:
            self._get_conflicted_entries()
        return self._conflict_entries

    @property
    def conflict_entries_by_path(self):
        if self._conflict_entries_by_path is None:
            self._get_conflicted_entries()
        return self._conflict_entries_by_path

    def _get_conflicted_entries(self):
        _conflict_entries = []
        _conflict_entries_by_path = {}
        for entry in self.entries:
            if not entry.has_conflict:
                continue
            _conflict_entries.append(entry)
            _conflict_entries_by_path[entry.path] = entry
        self._conflict_entries = _conflict_entries
        self._conflict_entries_by_path = _conflict_entries_by_path

    @property
    def has_conflict(self):
        return bool(self.conflict_entries)

    def has_non_props_changes(self):
        for entry in self.entries:
            if entry.has_non_props_changes:
                return True
        return False

    @property
    def unversionned(self):
        if self._unversionned is None:
            self._unversionned = [entry for entry in self.entries if not entry.is_unversionned]
        return self._unversionned


class InfoEntry(object):
    """Wrapper class for the <entry> items of svn info --xml"""

    def __init__(self, xml_element):
        self._xml = xml_element

        self._wc_info = False
        self._commit = False

    @property
    def path(self):
        return self._xml.attrib['path']

    @property
    def wc_info(self):
        if self._wc_info is False:
            self._wc_info = self._xml.find('wc-info')
        return self._wc_info

    @property
    def commit(self):
        if self._commit is False:
            self._commit = self._xml.find('commit')
        return self._commit

    @property
    def kind(self):
        return self._xml.attrib['kind']

    @property
    def is_file(self):
        return self.kind == 'file'

    @property
    def is_dir(self):
        return not self.is_file

    @property
    def url(self):
        return self._xml.find('url').text

    @property
    def repo_root(self):
        return self._xml.find('repository/root').text

    @property
    def repo_path(self):
        url = self.url
        root = self.repo_root.rstrip('/')
        if url.startswith(root):
            return '^' + url[len(root):]
        return url

    @property
    def tree_conflict(self):
        return self._xml.find('tree-conflict')


# TODO(stephane): make a base class for xml handling, <entry> is common to some of the commands.
class Info(object):
    """Wrapper class for 'svn info --xml' results."""

    def __init__(self, xml_element):
        self._xml = xml_element
        self._entries = None
        self._entries_by_path = None

    @property
    def entries(self):
        if self._entries is None:
            self._get_entries()
        return self._entries

    @property
    def entries_by_path(self):
        if self._entries_by_path is None:
            self._get_entries()
        return self._entries_by_path

    def _get_entries(self):
        _entries_by_path = {}
        _entries = []
        entries = [InfoEntry(x) for x in self._xml.findall('entry')]
        for entry in entries:
            if entry.path in _entries_by_path:
                continue
            _entries.append(entry)
            _entries_by_path[entry.path] = entry
        self._entries = _entries
        self._entries_by_path = _entries_by_path


class SvnWrapper(object):

    def __init__(self, auth=None, no_commit=False, verbose=False, stdout=None):
        if stdout is None:
            stdout = sys.stdout
        self._stdout = stdout
        self.no_commit = no_commit
        self.verbose = verbose
        self.auth = auth

        self._last_status = None

    @property
    def return_code(self):
        return self._last_status['return_code'] if self._last_status else None

    @property
    def stdout(self):
        return self._last_status['stdout'] if self._last_status else None

    @property
    def stderr(self):
        return self._last_status['stderr'] if self._last_status else None

    def run(self, options, discard_output=False):
        svn_cmd = ['svn', '--non-interactive']
        password = None
        if self.auth:
            svn_cmd += ['--username', self.auth.username]
            if self.auth.password:
                password = self.auth.password
                svn_cmd += ['--password', '%%PASSWORD%%']
        svn_cmd += options
        self._last_status = None
        self._last_status = execute_command(
            svn_cmd, discard_output=discard_output, verbose=self.verbose, stdout=self._stdout,
            password=password
        )
        return self.return_code

    def log(self, options):
        log_cmd = ['log'] + options
        return self.run(log_cmd)


def idle_merge_metacomment(revisions=None, mergeinfo_revisions=None):
    if revisions is None:
        revisions = []
    if mergeinfo_revisions is None:
        mergeinfo_revisions = []
    if type(revisions) is Revision:
        revisions = [revisions]
    comment = ['-- IDLEMERGE DATA --']
    if revisions:
        comment.append('REVISIONS=' + revisions_as_string(revisions, ','))
    if mergeinfo_revisions:
        comment.append('MERGEINFO_REVISIONS=' + revisions_as_string(mergeinfo_revisions, ','))
    all_revisions = sorted(revisions + mergeinfo_revisions)
    comment += ['r%s | %s | %s' % (r.number, r.author, r.date) for r in all_revisions]
    return '\n  '.join(comment)


class IdleMerge(object):

    def __init__(self, source, target='.', noop=True, single=False, verbose=False, stdout=None):
        if stdout is None:
            stdout = sys.stdout
        self.source = source
        self.target = target
        self._target_url = None
        self._stdout = stdout
        self.noop = noop
        self.no_merge_patterns = DEFAULT_NO_MERGE_PATTERNS
        self.single = single
        self.concise = False
        self.record_only_filename = None
        self.verbose = verbose
        self.validation_script = None
        # self.authentication = False
        # self.username = None
        # self.password = None
        # self.printOut = None
        self.svn = SvnWrapper(no_commit=noop, verbose=verbose, stdout=stdout)
        self._info = None

    @property
    def target_url(self):
        if self._target_url is None:
            self._target_url = self.info.entries_by_path[self.target].repo_path
        return self._target_url

    @property
    def info(self):
        if self._info is None:
            self.get_svn_info()
        return self._info

    def execute_svn_command(self, command_label):
        return self.svn.run(command_label, discard_output=False)

    def revert(self, options=None):
        if options is None:
            options = []
        return self.execute_svn_command(['revert'] + options + [self.target])

    def revert_all(self):
        return self.revert(['-R'])

    def svn_status(self, options=None):
        if options is None:
            options = []
        self.execute_svn_command(['status', '--xml'] + options + [self.target])
        return Status(xml.etree.ElementTree.fromstring(''.join(self.svn.stdout)))

    def svn_resolved(self, victim):
        return self.execute_svn_command(['resolved', victim])

    def get_svn_info(self, target=None):
        if target is None:
            target = self.target
        self.execute_svn_command(['info', '--xml', target])
        info = Info(xml.etree.ElementTree.fromstring(''.join(self.svn.stdout)))
        if target == self.target:
            self._info = info
        return info

    def svn_update(self):
        print "UPDATE"
        self._info = None
        return self.execute_svn_command(['update', self.target])

    def revert_pristine(self):
        """Revert all pending changes and delete unknown files to get a pristine working copy."""
        self.revert_all()
        self.svn_update()

        status = self.svn_status()
        if not status.unversionned:
            return
        # delete unversionned files
        for entry in status.unversionned:
            path = entry.path
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        if self.svn_update() == 0:
            raise Error('Failed to reset workspace !')

    def get_eligible_revisions(self):
        self.execute_svn_command([
            'mergeinfo', '--show-revs', 'eligible', self.source, self.target])
        svn_output = self.svn.stdout
        # TODO(stephane): add error handling, e.g.: if the branch name does not exist in the repo
        revision_re = re.compile(r'^r(\d+)$')
        revisions = []
        for line in svn_output:
            match = revision_re.match(line)
            if match:
                revisions.append(Revision(number=match.group(1), svn=self.svn, branch=self.source))
        return revisions

    # When this error happens, we want to update and retry the merge
    # svn: E195020: Cannot merge into mixed-revision working copy [431:432]; try updating first
    def svn_merge(self, revisions, merge_option='postpone'):
        if type(revisions) is Revision:
            revisions = [revisions]
        revisions_string = ','.join([str(revision.number) for revision in revisions])
        command = ['--accept', merge_option, 'merge', '-c', revisions_string,
            self.source, self.target]
        for _ in range(3):
            return_code = self.execute_svn_command(command)
            err_line = self.svn.stderr[0] if self.svn.stderr else ''
            if return_code and err_line.startswith('svn: E195020'):
                self.svn_update()
                continue
            break
        if return_code:
            print >> self._stdout, ' Executing %r failed !\n' % command
            print >> self._stdout, self.svn.stderr
            return False
        return True

    def commit(self, options=None):
        if options is None:
            options = []
        if self.noop:
            print 'NOOP: commit'
            return 0
        self.execute_svn_command(['commit'] + options + [self.target])
        print ''.join(self.svn.stdout)
        return self.svn.return_code

    def is_no_merge_revision(self, revision, record_only_revisions=None):
        if record_only_revisions and revision in record_only_revisions:
            return True
        comment = revision.msg
        for pattern in self.no_merge_patterns:
            if pattern in comment:
                return True
        return False

    def merge_record_only(self, revisions):
        revisions_string = revisions_as_string(revisions, ',')
        return self.execute_svn_command([
            'merge', '--accept', 'postpone', '--record-only', '-c', revisions_string,
            self.source, self.target
        ])

    # sample delete tree conflict.
    # <?xml version="1.0" encoding="UTF-8"?>
    # <info>
    # <entry
    #    kind="none"
    #    path="merge_file"
    #    revision="Resource is not under version control.">
    # <wc-info>
    # <schedule>normal</schedule>
    # <depth>unknown</depth>
    # </wc-info>
    # <tree-conflict
    #    operation="merge"
    #    kind="file"
    #    reason="delete"
    #    victim="merge_file"
    #    action="delete">
    # <version
    #    side="source-left"
    #    kind="file"
    #    path-in-repos="stephane/branches/stable/merge_file"
    #    repos-url="svn+ssh://svn.idle-games.com/var/svn/sandbox"
    #    revision="484"/>
    # <version
    #    side="source-right"
    #    kind="file"
    #    path-in-repos="stephane/branches/stable/merge_file"
    #    repos-url="svn+ssh://svn.idle-games.com/var/svn/sandbox"
    #    revision="485"/>
    # </tree-conflict>
    # </entry>
    # </info>

    def resolve_tree_conflict(self, tree_conflict):
        """Resolve a tree conflict on simple cases.

        A tree conflict section looks like this:
            <tree-conflict
                    operation="merge"
                    kind="file"
                    reason="delete"
                    victim="merge_file"
                    action="delete">
                <version
                    side="source-left"
                    kind="file"
                    path-in-repos="stephane/branches/stable/merge_file"
                    repos-url="svn+ssh://svn.idle-games.com/var/svn/sandbox"
                    revision="484"/>
                <version
                    side="source-right"
                    kind="file"
                    path-in-repos="stephane/branches/stable/merge_file"
                    repos-url="svn+ssh://svn.idle-games.com/var/svn/sandbox"
                    revision="485"/>
            </tree-conflict>
        """
        tc_attrib = tree_conflict.attrib
        action = tc_attrib['action']
        reason = tc_attrib['reason']
        victim = tree_conflict.attrib['victim']
        if action == 'delete' and reason == 'delete':
            if not self.svn_resolved(victim):
                print 'Resolved double delete conflict on %s' % victim
                return False
            return True
        if tc_attrib['action'] == 'delete' and tc_attrib['reason'] == 'edit':
            print 'Incoming delete but %s has been updated since last merge.' % victim
            return True
        print 'Conflict type not handled: action=%s, reason=%s on %s' % (action, reason, victim)
        return True

    def resolve_conflict(self, revision, conflict):
        """Resolve a conflict in the specific revision.

        Args:
            revision: A Revision() instance the the revision currently being merged.
            conflict: A StatusEntry() instance for the current conflict to handle."""
        target = conflict.path
        self.execute_svn_command(['info', '--xml', target])
        info = Info(xml.etree.ElementTree.fromstring(''.join(self.svn.stdout)))
        info_entry = info.entries[0]
        tree_conflict = info_entry.tree_conflict
        if tree_conflict:
            return self.resolve_tree_conflict(tree_conflict)
        return True

    def resolve_conflicts(self, revision):
        # Tree conflict, check if the file is the same on both sides.
        # Better would be to check if the file in target is 'newer', then 'accept-yours', if older
        # check svn diff --xml --internal-diff --summarize -N -r revision src_file tgt_file
        # or use svn cat | md5
        status = self.svn_status()
        conflicted = 0
        for conflict in status.conflict_entries:
            if self.resolve_conflict(revision, conflict):
                conflicted += 1
        return conflicted

    def get_source_sub_path(self, path):
        match = re.match(r'\^?(/.*?)/?(?:@.*)?$', self.source)
        if match:
            source = match.group(1) + '/'
        else:
            source = self.source
        if path.startswith(source):
            return path[len(source):]
        return path

    def revert_spurious_merges(self, revision, valid_entries=()):
        no_revert = set(valid_entries)
        for path_item in revision.paths:
            no_revert.add(self.get_source_sub_path(path_item.path))
        status = self.svn_status()
        to_revert = []
        for entry in status.entries:
            if entry.item == 'unversionned' or entry.path in no_revert:
                continue
            to_revert.append(entry.path)
        if not to_revert:
            return no_revert
        if self.verbose and no_revert:
            print 'Valid entries are:\n  ' + '\n  '.join(sorted(no_revert))
            print no_revert
        print 'Reverting spurious merges from %s on %s' % (revision, ' '.join(to_revert))
        self.execute_svn_command(['revert'] + to_revert)
        return no_revert

# merge revision r1234 by foo from ^/x to ^/bar: Original comment for the revision
#   on multiple lines
# -- IDLEMERGE DATA --
#   REVISIONS=1234
#   MERGEINFO_REVISIONS=1230,1233
# 
# merge revisions r1235,1236 from ^/x to ^/bar
#   -- IDLEMERGE DATA --
#   REVISIONS=1235,1236
#   MERGEINFO_REVISIONS=1230,1233
#   r39389 | _jenkins | 2012-02-17 17:13:35 -0800 (Fri, 17 Feb 2012)
#   r39389 | _jenkins | 2012-02-17 17:13:35 -0800 (Fri, 17 Feb 2012)

    def single_revision_message(self, revision, mergeinfo=False):
        return '[automerge %s@%s] %s' % (self.source, revision.number, revision.msg)

    def commit_log(self, revisions=None, mergeinfo_revisions=None):
        if revisions is None:
            revisions = []
        if mergeinfo_revisions is None:
            mergeinfo_revisions = []
        if type(revisions) is Revision:
            revisions = [revisions]

        if not revisions and not mergeinfo_revisions:
            raise Error('No revision provided')
        if len(revisions) == 1:
            message = self.single_revision_message(revisions[0])
        elif not revisions and len(mergeinfo_revisions) == 1:
            message = self.single_revision_message(mergeinfo_revisions[0], True)
        else:
            message = 'merge revisions %s from %s to %s' % (
                revisions_as_string(revisions), self.source, self.target_url)
        metacomment = idle_merge_metacomment(revisions, mergeinfo_revisions)
        return '%s\n%s' % (message, metacomment)

    def merge_one_by_one_concise(self, revisions, commit_mergeinfo=False):
        """Merge one by one but bundle bundle pure mergeinfo changes together to reduce noise.

        When merging this way, we do merge one revision that has a real diff one at a time, but
        bundle the empty merges together since SVN is not smart enough to not report merge backs.
        The pure mergeinfo revisions are pooled together until a real merge or a conflict is
        encountered.

        Args:
            revisions: A list of Revision() instances to be merged.
            commit_mergeinfo: A boolean, when set will force the commit even if a true merge or
                conflict is not reached. Default is False.
        """
        
        print 'Merging one by one, concise mode'
        record_only_revisions = self.load_record_only_revisions()
        if record_only_revisions:
            print 'Found %d revisions to record-only from previous run: %s' % (
                len(record_only_revisions), revisions_as_string(record_only_revisions))
        merged_paths = set(self.target)
        revisions_to_merge = revisions[:]
        mergeinfo_revisions = []
        while revisions_to_merge:
            print '=====> Merging: ' + revisions_as_string(revisions_to_merge)
            merged = []
            mergeinfo_revisions = []
            for revision in revisions_to_merge:
                if self.is_no_merge_revision(revision, record_only_revisions):
                    self.merge_record_only([revision])
                else:
                    self.svn_merge(revision)
                self.resolve_conflicts(revision)
                merged_paths = self.revert_spurious_merges(revision, merged_paths)
                status = self.svn_status()
                if status.has_conflict:
                    raise Conflict(
                        revision=revision, mergeinfos=mergeinfo_revisions, source=self.source)
                if status.has_non_props_changes():
                    merged = mergeinfo_revisions + [revision]
                    commit_log = self.commit_log(revision, mergeinfo_revisions)
                    print commit_log
                    self.commit(['-m', commit_log])
                    if not self.svn.return_code:
                        mergeinfo_revisions = []
                    break
                mergeinfo_revisions.append(revision)
            if mergeinfo_revisions == revisions_to_merge:
                if commit_mergeinfo:
                    Error('Not implemented')
                else:
                    print '=====> Only empty svn:mergeinfo to merge, skipping: %s' % ','.join([
                        str(r) for r in revisions_to_merge])
                    self.save_record_only_revisions(mergeinfo_revisions)
                    return None
            revisions_to_merge = [r for r in revisions_to_merge if r not in merged]
        self.save_record_only_revisions(mergeinfo_revisions)
        return None

    def merge_one_by_one(self, revisions):
        for revision in revisions:
            if self.is_no_merge_revision(revision):
                return_code = self.merge_record_only([revision])
            else:
                return_code = self.svn_merge([revision])
            if return_code:
                print 'Error %s returned when merging' % return_code

    def load_record_only_revisions(self):
        if not self.record_only_filename or not os.path.exists(self.record_only_filename):
            return []
        with open(self.record_only_filename, 'r') as records_file:
            revisions = []
            for line in records_file:
                revisions += [Revision(r.strip()) for r in line.split(',') if r.strip()]
            revisions.sort()
            return revisions

    def save_record_only_revisions(self, revisions):
        if not self.record_only_filename:
            return
        print 'Saving record-only revisions to %s: %s' % (
            self.record_only_filename, revisions_as_string(revisions, ','))
        with open(self.record_only_filename, 'w') as records_file:
            print >> records_file, revisions_as_string(revisions, ',')

    def launch_merge(self):
        """launch the merge

        Returns:
            A boolean - true if everything went fine or false if manual merge to be done.
        """

        self.revert_pristine()
        revisions = self.get_eligible_revisions()
        print >> self._stdout, 'Merging %s revisions ...' % len(revisions)

        try:
            if self.single:
                if self.concise:
                    self.merge_one_by_one_concise(revisions)
                else:
                    self.merge_one_by_one(revisions)
            else:
                raise Error('Not implemented')
        except Conflict as conflict:
            print str(conflict)
            svn_status = execute_command(['svn', 'status'])
            print ''.join(svn_status['stdout'])
            self.save_record_only_revisions(conflict.mergeinfos)
            return 1
        print 'Done merging'
        return 0


def extract_additional_patterns(patterns_string):
    if patterns_string:
        return [x.strip() for x in patterns_string.split(',') if x.strip()]

    patterns = []
    patterns_filepath = 'patterns.txt' # if not options.patternsFile else options.patternsFile
    if not os.path.exists(patterns_filepath):
        return
    patterns_file = open(patterns_filepath)
    for line in patterns_file:
        pattern = line.strip()
        if not pattern or pattern.startswith('#'):
            continue
        patterns.append(pattern)
    return patterns


def main(argv):
    try:
        options = parse_args(argv)
    except Error:
        return 1

    noop = options.noop
    single = options.single
    source_url = options.source
    verbose = options.verbose

    # validation_script = options.validation
    # additional_patterns = extract_additional_patterns(options.patterns)

    idlemerge = IdleMerge(source_url, noop=noop, single=single, verbose=verbose)
    idlemerge.concise = options.concise
    idlemerge.record_only_filename = options.record_only_filename
    return idlemerge.launch_merge()


if __name__ == '__main__':
    sys.exit(main(sys.argv))
