# -*- coding: utf-8 -*-
# Generated by Django 1.11 on 2018-11-27 20:31
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('server', '0141_wfmodule_is_deleted_data'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='wfmodule',
            name='cached_render_result_workflow_id',
        ),
    ]
