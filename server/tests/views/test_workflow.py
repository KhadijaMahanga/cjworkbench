import asyncio
from collections import namedtuple
import json
from unittest.mock import patch
from allauth.account.utils import user_display
from django.contrib.auth.models import AnonymousUser
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate
from server.models import ModuleVersion, User, Workflow
from server.models.commands import InitWorkflowCommand
from server.tests.utils import LoggedInTestCase
from server.views import workflow_list, workflow_detail, render_workflow, \
        render_workflows


FakeSession = namedtuple('FakeSession', ['session_key'])


async def async_noop(*args, **kwargs):
    pass


future_none = asyncio.Future()
future_none.set_result(None)


@patch('server.models.Delta.schedule_execute', async_noop)
@patch('server.models.Delta.ws_notify', async_noop)
class WorkflowViewTests(LoggedInTestCase):
    def setUp(self):
        super().setUp()  # log in

        self.log_patcher = patch('server.utils.log_user_event_from_request')
        self.log_patch = self.log_patcher.start()

        self.factory = APIRequestFactory()
        self.workflow1 = Workflow.objects.create(name='Workflow 1',
                                                 owner=self.user)
        self.delta = InitWorkflowCommand.create(self.workflow1)
        self.tab1 = self.workflow1.tabs.create(position=0)
        self.module_version1 = ModuleVersion.create_or_replace_from_spec({
            'id_name': 'module1',
            'name': 'Module 1',
            'category': 'Cat',
            'parameters': []
        })

        # Add another user, with one public and one private workflow
        self.otheruser = User.objects.create(username='user2',
                                             email='user2@users.com',
                                             password='password')
        self.other_workflow_private = Workflow.objects.create(
            name="Other workflow private",
            owner=self.otheruser
        )
        self.other_workflow_public = Workflow.objects.create(
            name="Other workflow public",
            owner=self.otheruser,
            public=True
        )

    def tearDown(self):
        self.log_patcher.stop()
        super().tearDown()

    def _augment_request(self, request, user: User, session_key: str) -> None:
        if user:
            force_authenticate(request, user=user)
        request.session = FakeSession(session_key)
        request.user = user

    def _build_delete(self, *args, user: User=None, session_key: str='a-key',
                      **kwargs):
        request = self.factory.delete(*args, **kwargs)
        self._augment_request(request, user, session_key)
        return request

    def _build_get(self, *args, user: User=None, session_key: str='a-key',
                   **kwargs):
        request = self.factory.get(*args, **kwargs)
        self._augment_request(request, user, session_key)
        return request

    def _build_patch(self, *args, user: User=None, session_key: str='a-key',
                     **kwargs):
        request = self.factory.patch(*args, **kwargs)
        self._augment_request(request, user, session_key)
        return request

    def _build_post(self, *args, user: User=None, session_key: str='a-key',
                    **kwargs):
        request = self.factory.post(*args, **kwargs)
        self._augment_request(request, user, session_key)
        return request

    def _build_put(self, *args, user: User=None, session_key: str='a-key',
                   **kwargs):
        request = self.factory.put(*args, **kwargs)
        self._augment_request(request, user, session_key)
        return request

    # --- Workflow list ---
    def test_workflow_list_get(self):
        # set dates to test reverse chron ordering
        self.workflow1.creation_date = "2010-10-20 1:23Z"
        self.workflow1.save()
        self.workflow2 = Workflow.objects.create(
            name='Workflow 2',
            owner=self.user,
            creation_date='2015-09-18 2:34Z'
        )

        request = self._build_get('/api/workflows/', user=self.user)
        response = render_workflows(request)
        self.assertIs(response.status_code, status.HTTP_200_OK)

        workflows = response.context_data['initState']['workflows']
        # should not pick up other user's workflows, even public ones
        self.assertEqual(len(workflows['owned']) + len(workflows['shared']) + len(workflows['templates']), 2)

        self.assertEqual(workflows['owned'][0]['name'], 'Workflow 2')
        self.assertEqual(workflows['owned'][0]['id'], self.workflow2.id)
        self.assertEqual(workflows['owned'][0]['public'], self.workflow1.public)
        self.assertEqual(workflows['owned'][0]['read_only'], False)  # user is owner
        self.assertEqual(workflows['owned'][0]['is_owner'], True)  # user is owner
        self.assertIsNotNone(workflows['owned'][0]['last_update'])
        self.assertEqual(workflows['owned'][0]['owner_name'], user_display(self.workflow2.owner))

        self.assertEqual(workflows['owned'][1]['name'], 'Workflow 1')
        self.assertEqual(workflows['owned'][1]['id'], self.workflow1.id)

    def test_workflow_list_include_example_in_all_users_workflow_lists(self):
        self.other_workflow_public.example = True
        self.other_workflow_public.in_all_users_workflow_lists = True
        self.other_workflow_public.save()
        self.workflow2 = Workflow.objects.create(owner=self.user)

        request = self._build_get('/api/workflows/', user=self.user)
        response = render_workflows(request)
        self.assertIs(response.status_code, status.HTTP_200_OK)

        workflows = response.context_data['initState']['workflows']
        self.assertEqual(len(workflows['owned']), 2)
        self.assertEqual(len(workflows['templates']), 1)

    def test_workflow_list_exclude_example_not_in_all_users_lists(self):
        self.other_workflow_public.example = True
        self.other_workflow_public.in_all_users_workflow_lists = False
        self.other_workflow_public.save()
        self.workflow2 = Workflow.objects.create(owner=self.user)

        request = self._build_get('/api/workflows/', user=self.user)
        response = render_workflows(request)
        self.assertIs(response.status_code, status.HTTP_200_OK)

        workflows = response.context_data['initState']['workflows']
        self.assertEqual(len(workflows['owned']), 2)
        self.assertEqual(len(workflows['templates']), 0)

    def test_workflow_list_exclude_lesson(self):
        self.workflow1.lesson_slug = 'some-lesson'
        self.workflow1.save()
        self.workflow2 = Workflow.objects.create(owner=self.user)

        request = self._build_get('/api/workflows/', user=self.user)
        response = render_workflows(request)
        workflows = response.context_data['initState']['workflows']
        self.assertIs(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(workflows['owned']), 1)

    def test_workflow_list_post(self):
        start_count = Workflow.objects.count()
        request = self._build_post('/api/workflows/', user=self.user)
        response = workflow_list(request)
        self.assertIs(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Workflow.objects.count(), start_count+1)
        self.assertEqual(Workflow.objects.filter(name='New Workflow').count(), 1)

    # --- Workflow ---
    # This is the HTTP response, as opposed to the API
    def test_workflow_view(self):
        # View own non-public workflow
        self.client.force_login(self.user)
        response = self.client.get('/workflows/%d/' % self.workflow1.id)  # need trailing slash or 301
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch('server.rabbitmq.queue_render')
    def test_workflow_view_triggers_render(self, queue_render):
        queue_render.return_value = future_none
        self.tab1.wf_modules.create(
            order=0,
            last_relevant_delta_id=self.delta.id,
            cached_render_result_delta_id=-1  # stale
        )
        self.client.force_login(self.user)
        self.client.get('/workflows/%d/' % self.workflow1.id)
        queue_render.assert_called_with(self.workflow1.id, self.delta.id)

    @patch('server.rabbitmq.queue_render')
    def test_workflow_view_triggers_render_if_no_cache(self, queue_render):
        queue_render.return_value = future_none
        self.tab1.wf_modules.create(
            order=0,
            last_relevant_delta_id=self.delta.id,
            cached_render_result_delta_id=None
        )
        self.client.force_login(self.user)
        self.client.get('/workflows/%d/' % self.workflow1.id)
        queue_render.assert_called_with(self.workflow1.id, self.delta.id)

    def test_workflow_view_missing_404(self):
        # 404 with bad id
        self.client.force_login(self.user)
        response = self.client.get('/workflows/%d/' % 999999)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_workflow_view_shared(self):
        # View someone else's public workflow
        self.client.force_login(self.user)
        self.assertTrue(self.other_workflow_public.public)
        response = self.client.get('/workflows/%d/' % self.workflow1.id)
        self.assertIs(response.status_code, status.HTTP_200_OK)

    def test_workflow_view_unauthorized_403(self):
        # 403 viewing someone else' private workflow (don't 404 as sometimes
        # users try to share workflows by sharing the URL without first making
        # them public, and we need to help them debug that case)
        self.client.force_login(self.otheruser)
        response = self.client.get('/workflows/%d/' % self.workflow1.id)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_workflow_init_state(self):
        # checks to make sure the right initial data is embedded in the HTML (username etc.)
        with patch.dict('os.environ', { 'CJW_INTERCOM_APP_ID':'myIntercomId', 'CJW_GOOGLE_ANALYTICS':'myGaId'}):
            response = self.client.get('/workflows/%d/' % self.workflow1.id)  # need trailing slash or 301
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            self.assertContains(response, '"loggedInUser"')
            self.assertContains(response, user_display(self.user))
            self.assertContains(response, self.user.email)

            self.assertContains(response, '"workflow"')
            self.assertContains(response, '"modules"')

            self.assertContains(response, 'myIntercomId')
            self.assertContains(response, 'myGaId')

    def test_workflow_acl_reader_reads_but_does_not_write(self):
        self.workflow1.acl.create(email='user2@users.com', can_edit=False)
        self.client.force_login(self.otheruser)

        # GET: works
        response = self.client.get(f'/workflows/{self.workflow1.id}/')
        self.assertEqual(response.status_code, 200)

        # POST: does not work
        response = self.client.post(f'/api/workflows/{self.workflow1.id}/',
                                    data='{"public":true}',
                                    content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_workflow_acl_writer_reads_and_writes(self):
        self.workflow1.acl.create(email='user2@users.com', can_edit=True)
        self.client.force_login(self.otheruser)

        # GET: works
        response = self.client.get(f'/workflows/{self.workflow1.id}/')
        self.assertEqual(response.status_code, 200)

        # POST: works
        response = self.client.post(f'/api/workflows/{self.workflow1.id}/',
                                    data='{"public":true}',
                                    content_type='application/json')
        self.assertEqual(response.status_code, 204)

    def test_workflow_anonymous_user(self):
        # Looking at example workflow as anonymous should create a new workflow
        num_workflows = Workflow.objects.count()

        self.other_workflow_public.example = True
        self.other_workflow_public.save()

        # Also ensure the anonymous users can't access the Python module; first we need to load it
        ModuleVersion.create_or_replace_from_spec({
            'id_name': 'pythoncode',
            'name': 'Python',
            'category': 'not allowed',
            'parameters': [],
        })

        request = self._build_get('/workflows/%d/' % self.other_workflow_public.id, user=AnonymousUser())
        response = render_workflow(request, workflow_id=self.other_workflow_public.id)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(Workflow.objects.count(), num_workflows + 1)  # should have duplicated the  wf with this API call

        # Ensure the anonymous users can't access the Python module
        self.assertNotContains(response, '"pythoncode"')

    def test_workflow_duplicate_view(self):
        old_ids = [w.id for w in Workflow.objects.all()] # list of all current workflow ids
        response = self.client.post('/api/workflows/%d/duplicate' % self.workflow1.id)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = json.loads(response.content)
        self.assertFalse(data['id'] in old_ids) # created at entirely new id
        self.assertEqual(data['name'], 'Copy of Workflow 1')
        self.assertTrue(Workflow.objects.filter(pk=data['id']).exists())

    def test_workflow_duplicate_missing_gives_404(self):
        # Ensure 404 with bad id
        response = self.client.post('/api/workflows/99999/duplicate')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_workflow_duplicate_restricted_gives_403(self):
        # Ensure 403 when another user tries to clone private workflow
        self.assertFalse(self.workflow1.public)
        self.client.force_login(self.otheruser)
        response = self.client.post('/api/workflows/%d/duplicate' % self.workflow1.id)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_workflow_duplicate_public(self):
        self.workflow1.public = True
        self.workflow1.save()
        response = self.client.post('/api/workflows/%d/duplicate'
                                    % self.workflow1.id)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_workflow_detail_delete(self):
        pk_workflow = self.workflow1.id
        request = self._build_delete('/api/workflows/%d/' % pk_workflow,
                                     user=self.user)
        response = workflow_detail(request, workflow_id=pk_workflow)
        self.assertIs(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Workflow.objects.filter(name='Workflow 1').count(), 0)

    def test_workflow_public_post(self):
        pk_workflow = self.workflow1.id
        request = self._build_post('/api/workflows/%d' % pk_workflow,
                                   {'public': True}, user=self.user)
        response = workflow_detail(request, workflow_id=pk_workflow)
        self.assertIs(response.status_code, status.HTTP_204_NO_CONTENT)
        self.workflow1.refresh_from_db()
        self.assertEqual(self.workflow1.public, True)
