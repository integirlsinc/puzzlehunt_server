# -*- coding: utf-8 -*-
# Generated by Django 1.11.15 on 2019-01-09 16:55
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('huntserver', '0029_auto_20190108_1429'),
    ]

    operations = [
        migrations.AddField(
            model_name='prepuzzle',
            name='released',
            field=models.BooleanField(default=False),
        ),
    ]