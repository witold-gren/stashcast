import logging
import os
import shutil

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from media.models import MediaItem

logger = logging.getLogger(__name__)


@receiver(pre_delete, sender=MediaItem)
def cleanup_media_files(sender, instance, **kwargs):
    """
    Delete associated files and directory when a MediaItem is deleted.

    pre_delete (rather than a delete() override) is deliberate: it also fires for bulk
    deletions via queryset.delete(), which never calls the model's delete() method -
    including the admin's "delete selected" action.

    Registered by MediaConfig.ready(); without that import nothing calls this.
    """
    base_dir = instance.get_base_dir()
    if base_dir and os.path.exists(base_dir):
        try:
            shutil.rmtree(base_dir)
            logger.info('Deleted media directory %s', base_dir)
        except Exception as e:
            # Log and continue: a failed rmtree must not block the DB deletion
            logger.error('Error deleting directory %s: %s', base_dir, e)
