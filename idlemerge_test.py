#!/usr/bin/env python2.7
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

"""Unittests for idlemerge.py."""

import idlemerge
import mock
import mox
import unittest
import xml.etree.ElementTree


class testAddEmailDomain(unittest.TestCase):

    def test_no_domain(self):
        self.assertEqual('foo', idlemerge.add_email_domain('foo', ''))

    def test_with_existing_at(self):
        self.assertEqual('foo@baz', idlemerge.add_email_domain('foo@baz', 'bar'))

    def test_ends_with_domain(self):
        self.assertEqual('foo@bar', idlemerge.add_email_domain('foo', 'bar'))

    def test_ends_with_at_domain(self):
        self.assertEqual('foo@bar', idlemerge.add_email_domain('foo', '@bar'))

    def test_ends_with_domain_braqueted(self):
        self.assertEqual(
            'Foo Bar <foo@bar>', idlemerge.add_email_domain('Foo Bar <foo@bar>', 'bar'))

    def test_ends_with_at_domain_braqueted(self):
        self.assertEqual(
            'Foo Bar <foo@bar>', idlemerge.add_email_domain('Foo Bar <foo@bar>', '@bar'))

    def test_append_domain(self):
        self.assertEqual('foo@bar', idlemerge.add_email_domain('foo', 'bar'))

    def test_append_at_domain(self):
        self.assertEqual('foo@bar', idlemerge.add_email_domain('foo', '@bar'))


class testMergeEmailRecipientsForConflict(unittest.TestCase):

    def setUp(self):
        self.mox = mox.Mox()
        self.mock_conflict = self.mox.CreateMockAnything()
        self.mock_revision = self.mox.CreateMockAnything()

    def tearDown(self):
        self.mox.VerifyAll()
        self.mox.UnsetStubs()

    def test_no_recipients(self):
        revision = self.mock_revision
        conflict = self.mock_conflict
        revision.author = None
        conflict.revision = revision
        self.mox.ReplayAll()

        expected = set()
        merge_email = idlemerge.MergeEmail('conflict', '@localhost', None, 'fake_sender', None)
        received = merge_email.recipients_for_conflict(conflict)
        self.assertEqual(expected, received)

    def test_add_author_to_empty_default(self):
        revision = self.mock_revision
        conflict = self.mock_conflict
        revision.author = 'foo'
        conflict.revision = revision
        self.mox.ReplayAll()

        expected = set(['foo@localhost'])
        merge_email = idlemerge.MergeEmail('conflict', '@localhost', None, 'fake_sender', None)
        received = merge_email.recipients_for_conflict(conflict)
        self.assertEqual(expected, received)

    def test_add_author_once(self):
        revision = self.mock_revision
        conflict = self.mock_conflict
        revision.author = 'foo'
        conflict.revision = revision
        self.mox.ReplayAll()

        expected = set(['foo@localhost', 'bar@localhost'])
        merge_email = idlemerge.MergeEmail(
            'conflict', '@localhost', ['foo@localhost', 'bar'], 'fake_sender', None)
        received = merge_email.recipients_for_conflict(conflict)
        self.assertEqual(expected, received)

    def test_author_and_string_default(self):
        revision = self.mock_revision
        conflict = self.mock_conflict
        revision.author = 'foo'
        conflict.revision = revision
        self.mox.ReplayAll()

        expected = set(['foo@localhost', 'bar@localhost'])
        merge_email = idlemerge.MergeEmail(
            'conflict', '@localhost', 'bar', 'fake_sender', None)
        received = merge_email.recipients_for_conflict(conflict)
        self.assertEqual(expected, received)


class testCommitLog(unittest.TestCase):

    def setUp(self):
        self.source_url = '^/foo/stable'
        self.revision1 = idlemerge.Revision(xml_element=xml.etree.ElementTree.fromstring(
            '<logentry revision="1">'
            '<author>foo</author>'
            '<date>2011-01-01T01:01:01.100000Z</date>'
            '<msg>log message for revision 1</msg>'
            ' </logentry>'))
        self.revision2 = idlemerge.Revision(xml_element=xml.etree.ElementTree.fromstring(
            '<logentry revision="2">'
            '<author>bar</author>'
            '<date>2012-02-02T02:02:02.200000Z</date>'
            '<msg>log message for revision 2</msg>'
            ' </logentry>'))

    def test_one_diff_revision(self):
        idlemerge_instance = idlemerge.IdleMerge(self.source_url)
        expected = ('[automerge ^/foo/stable@1] log message for revision 1\n'
                    '-- IDLEMERGE DATA --\n'
                    '  REVISIONS=1\n'
                    '  r1 | foo | 2011-01-01 01:01:01.100000')
        received = idlemerge_instance.commit_log(revisions=self.revision1)
        self.assertEqual(expected, received)

    def test_one_mergeinfo_revision(self):
        idlemerge_instance = idlemerge.IdleMerge(self.source_url)
        expected = ('[automerge ^/foo/stable@1] log message for revision 1\n'
                    '-- IDLEMERGE DATA --\n'
                    '  MERGEINFO_REVISIONS=1\n'
                    '  r1 | foo | 2011-01-01 01:01:01.100000')
        received = idlemerge_instance.commit_log(mergeinfo_revisions=[self.revision1])
        self.assertEqual(expected, received)

    def test_multiple_mergeinfo_revisions(self):
        idlemerge_instance = idlemerge.IdleMerge(self.source_url)
        expected = ('[automerge ^/foo/stable] Committing mergeinfo changes\n'
                    '-- IDLEMERGE DATA --\n'
                    '  MERGEINFO_REVISIONS=1,2\n'
                    '  r1 | foo | 2011-01-01 01:01:01.100000\n'
                    '  r2 | bar | 2012-02-02 02:02:02.200000')
        received = idlemerge_instance.commit_log(
            mergeinfo_revisions=[self.revision1, self.revision2])
        self.assertEqual(expected, received)

    def test_multiple_diff_revisions(self):
        with mock.patch('idlemerge.IdleMerge.target_url',
                        new_callable=mock.PropertyMock) as mock_target_url:
            mock_target_url.return_value = '^/foo/trunk'
            idlemerge_instance = idlemerge.IdleMerge(self.source_url)
            expected = ('merge revisions 1, 2 from ^/foo/stable to ^/foo/trunk\n'
                        '-- IDLEMERGE DATA --\n'
                        '  REVISIONS=1,2\n'
                        '  r1 | foo | 2011-01-01 01:01:01.100000\n'
                        '  r2 | bar | 2012-02-02 02:02:02.200000')
            received = idlemerge_instance.commit_log(revisions=[self.revision1, self.revision2])
            self.assertEqual(expected, received)

    def test_diff_and_mergeinfo_revisions(self):
        idlemerge_instance = idlemerge.IdleMerge(self.source_url)
        expected = ('[automerge ^/foo/stable@1] log message for revision 1\n'
                    '-- IDLEMERGE DATA --\n'
                    '  REVISIONS=1\n'
                    '  MERGEINFO_REVISIONS=2\n'
                    '  r1 | foo | 2011-01-01 01:01:01.100000\n'
                    '  r2 | bar | 2012-02-02 02:02:02.200000')
        received = idlemerge_instance.commit_log(
            revisions=self.revision1, mergeinfo_revisions=[self.revision2])
        self.assertEqual(expected, received)



if __name__ == '__main__':
    unittest.main()
