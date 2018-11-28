from channels.db import database_sync_to_async
from django.db import models
from server.models import Delta, WfModule
from server import websockets
from .util import ChangesWfModuleOutputs


@database_sync_to_async
def _workflow_has_notifications(workflow):
    """Detect whether a workflow sends email on changes."""
    return workflow.live_wf_modules.filter(notifications=True).exists()


class ChangeDataVersionCommand(Delta, ChangesWfModuleOutputs):
    # TODO set null=False. null=True makes no sense.
    wf_module = models.ForeignKey(WfModule, null=True, default=None,
                                  blank=True, on_delete=models.PROTECT)
    # may not have had a previous version
    old_version = models.DateTimeField('old_version', null=True)
    new_version = models.DateTimeField('new_version')
    dependent_wf_module_last_delta_ids = \
        ChangesWfModuleOutputs.dependent_wf_module_last_delta_ids
    wf_module_delta_ids = ChangesWfModuleOutputs.wf_module_delta_ids

    def forward_impl(self):
        self.wf_module.stored_data_version = self.new_version
        self.wf_module.save(update_fields=['stored_data_version'])
        self.forward_affected_delta_ids(self.wf_module)

    def backward_impl(self):
        self.wf_module.stored_data_version = self.old_version
        self.wf_module.save(update_fields=['stored_data_version'])
        self.backward_affected_delta_ids(self.wf_module)

    async def schedule_execute(self) -> None:
        """
        Tell renderers to render the new workflow, _maybe_.

        ChangeDataVersionCommand is often created from a fetch, and fetches
        are often invoked by cron. These tend to be our most resource-heavy
        operations: e.g., Twitter-accumulate with 1M records. So let's use lazy
        rendering.

        From our point of view:

            * If self.workflow has notifications, render.
            * If anybody is viewing self.workflow right now, render.

        Of course, it's impossible for us to know whether anybody is viewing
        self.workflow. So we _broadcast_ to them and ask _them_ to request a
        render if they're listening. This gives N render requests (one per
        Websockets cconsumer) instead of 1, but we don't mind because
        spurious render requests are no-ops.

        From the user's point of view:

            * If I'm viewing self.workflow, changing data versions causes a
              render. (There isn't even any HTTP traffic: the consumer does
              the work.)
            * Otherwise, the next time I browse to the page, the page-load
              will request a render.

        Assumptions:

            * Websockets cconsumers queue a render when we ask them.
            * The Django page-load view queues a render when needed.
        """
        if await _workflow_has_notifications(self.workflow):
            await super().schedule_execute()
        else:
            await websockets.queue_render_if_listening(
                self.workflow.id,
                self.workflow.last_delta_id
            )

    @classmethod
    def amend_create_kwargs(cls, *, wf_module, **kwargs):
        return {
            **kwargs,
            'wf_module': wf_module,
            'old_version': wf_module.stored_data_version,
            'wf_module_delta_ids': cls.affected_wf_module_delta_ids(wf_module),
        }

    @classmethod
    async def create(cls, wf_module, version):
        return await cls.create_impl(
            workflow=wf_module.workflow,
            wf_module=wf_module,
            new_version=version
        )

    @property
    def command_description(self):
        return (
            f'Change {self.wf_module.get_module_name()} data version to '
            f'{self.version}'
        )
