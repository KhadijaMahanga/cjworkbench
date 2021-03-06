from unittest.mock import patch
from django.contrib.auth.models import User
from server.handlers.workflow import set_name, set_position, set_tab_order, \
    set_selected_tab
from server.models import Workflow
from server.models.commands import ChangeWorkflowTitleCommand
from .util import HandlerTestCase


async def async_noop(*args, **kwargs):
    pass


class WorkflowTest(HandlerTestCase):
    @patch('server.websockets.ws_client_send_delta_async', async_noop)
    def test_set_name(self):
        user = User.objects.create(username='a', email='a@example.org')
        workflow = Workflow.create_and_init(owner=user, name='A')

        response = self.run_handler(set_name, user=user, workflow=workflow,
                                    name='B')
        self.assertResponse(response, data=None)

        command = ChangeWorkflowTitleCommand.objects.first()
        self.assertEqual(command.new_value, 'B')
        self.assertEqual(command.old_value, 'A')

        workflow.refresh_from_db()
        self.assertEqual(workflow.name, 'B')

    def test_set_name_viewer_access_denied(self):
        workflow = Workflow.create_and_init(public=True)

        response = self.run_handler(set_name, workflow=workflow, name='B')
        self.assertResponse(response,
                            error='AuthError: no write access to workflow')

    @patch('server.websockets.ws_client_send_delta_async', async_noop)
    def test_set_name_coerce_to_str(self):
        user = User.objects.create(username='a', email='a@example.org')
        workflow = Workflow.create_and_init(owner=user, name='A')

        response = self.run_handler(set_name, user=user, workflow=workflow,
                                    name=['B', {'x': 'y'}])
        self.assertResponse(response, data=None)

        workflow.refresh_from_db()
        self.assertEqual(workflow.name, "['B', {'x': 'y'}]")

    def test_set_position(self):
        user = User.objects.create(username='a', email='a@example.org')
        workflow = Workflow.create_and_init(owner=user)
        tab2 = workflow.tabs.create(position=1)
        tab2.wf_modules.create(order=0)
        tab2.wf_modules.create(order=1)
        wf_module = tab2.wf_modules.create(order=2)

        response = self.run_handler(set_position, user=user, workflow=workflow,
                                    wfModuleId=wf_module.id)
        self.assertResponse(response, data=None)

        workflow.refresh_from_db()
        self.assertEqual(workflow.selected_tab_position, 1)
        tab2.refresh_from_db()
        self.assertEqual(tab2.selected_wf_module_position, 2)

    def test_set_position_viewer_access_denied(self):
        workflow = Workflow.create_and_init(public=True)
        wf_module = workflow.tabs.first().wf_modules.create(order=0)

        response = self.run_handler(set_position, workflow=workflow,
                                    wfModuleId=wf_module.id)
        self.assertResponse(response,
                            error='AuthError: no write access to workflow')

    def test_set_position_ignore_other_workflow(self):
        # (Also tests "ignore missing WfModule")
        user = User.objects.create(username='a', email='a@example.org')
        workflow = Workflow.create_and_init(owner=user)

        workflow2 = Workflow.create_and_init(owner=user)
        tab2 = workflow2.tabs.first()
        tab2.wf_modules.create(order=0)  # dummy first module (selected)
        wf_module = tab2.wf_modules.create(order=1)  # module we'll "select"
        tab2.selected_wf_module_position = 0
        tab2.save(update_fields=['selected_wf_module_position'])

        response = self.run_handler(set_position, user=user, workflow=workflow,
                                    wfModuleId=wf_module.id)
        self.assertResponse(response, data=None)  # we ignore missing steps
        # Nothing should be written to workflow2. Also, there's nothing to
        # write to workflow. So nothing in the DB should have changed.
        #
        # We don't report an error because there's a race: Alice deletes
        # module, and Bob clicks it as Alice is deleting it. We want to ignore
        # Bob's action in that case.
        tab2.refresh_from_db()
        self.assertEqual(tab2.selected_wf_module_position, 0)

    def test_set_selected_tab(self):
        user = User.objects.create(username='a', email='a@example.org')
        workflow = Workflow.create_and_init(owner=user)
        workflow.tabs.create(position=1, slug='tab-2')

        response = self.run_handler(set_selected_tab, user=user,
                                    workflow=workflow, tabSlug='tab-2')
        self.assertResponse(response, data=None)

        workflow.refresh_from_db()
        self.assertEqual(workflow.selected_tab_position, 1)

    def test_set_selected_tab_viewer_access_denied(self):
        workflow = Workflow.create_and_init(public=True)
        workflow.tabs.create(position=1, slug='tab-2')

        response = self.run_handler(set_selected_tab, workflow=workflow,
                                    tabSlug='tab-2')
        self.assertResponse(response,
                            error='AuthError: no write access to workflow')

    def test_set_selected_tab_ignore_other_workflow(self):
        user = User.objects.create(username='a', email='a@example.org')
        workflow = Workflow.create_and_init(owner=user)

        workflow2 = Workflow.create_and_init(owner=user)
        workflow2.tabs.create(position=1, slug='tab-2')

        response = self.run_handler(set_selected_tab, user=user,
                                    workflow=workflow,
                                    tabSlug='tab-2')
        self.assertResponse(response, error='Invalid tab slug')

    @patch('server.websockets.ws_client_send_delta_async', async_noop)
    def test_set_tab_order(self):
        user = User.objects.create(username='a', email='a@example.org')
        workflow = Workflow.create_and_init(owner=user)  # initial tab: tab-1
        workflow.tabs.create(position=1, slug='tab-2')

        response = self.run_handler(set_tab_order, user=user,
                                    workflow=workflow,
                                    tabSlugs=['tab-2', 'tab-1'])
        self.assertResponse(response, data=None)

        self.assertEqual(
            list(workflow.live_tabs.values_list('slug', flat=True)),
            ['tab-2', 'tab-1']
        )

    def test_set_tab_order_viewer_access_denied(self):
        workflow = Workflow.create_and_init()  # tab-1
        workflow.tabs.create(position=1, slug='tab-2')

        response = self.run_handler(set_tab_order, workflow=workflow,
                                    tabSlugs=['tab-2', 'tab-1'])
        self.assertResponse(response,
                            error='AuthError: no write access to workflow')

    def test_set_tab_order_wrong_tab_slugs(self):
        user = User.objects.create(username='a', email='a@example.org')
        workflow = Workflow.create_and_init(owner=user)  # tab-1
        workflow.tabs.create(position=1, slug='tab-2')

        response = self.run_handler(set_tab_order, user=user,
                                    workflow=workflow,
                                    tabSlugs=['tab-3', 'tab-2'])
        self.assertResponse(response, error='wrong tab slugs')

    def test_set_tab_order_invalid_tab_ids(self):
        user = User.objects.create(username='a', email='a@example.org')
        workflow = Workflow.create_and_init(owner=user)

        response = self.run_handler(set_tab_order, user=user,
                                    workflow=workflow, tabSlugs=[1, 2])
        self.assertResponse(response,
                            error='tabSlugs must be an Array of slugs')
