# Generated by Django 3.2.12 on 2023-11-23 12:51

from django.db import migrations
import paasng.platform.bkapp_model.models


class Migration(migrations.Migration):

    dependencies = [
        ('bkapp_model', '0008_domainresolution_svcdiscconfig'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='moduleprocessspec',
            options={'ordering': ['id']},
        ),
        migrations.AlterField(
            model_name='moduleprocessspec',
            name='scaling_config',
            field=paasng.platform.bkapp_model.models.AutoscalingConfigField(null=True, verbose_name='自动扩缩容配置'),
        ),
        migrations.AlterField(
            model_name='processspecenvoverlay',
            name='scaling_config',
            field=paasng.platform.bkapp_model.models.AutoscalingConfigField(null=True, verbose_name='自动扩缩容配置'),
        ),
    ]
