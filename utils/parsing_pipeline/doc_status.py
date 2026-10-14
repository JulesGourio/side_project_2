"""Document statuses of processed_files, shared by the parse steps and the description step (no Spark import).

3_parse leaves a document in a terminal status, which the next incremental run does not parse again. A document
with no text but images is IMAGES_PENDING while its images are not described; 4_describe_images moves it on (SUCCESS
or SKIPPED_EMPTY_IMAGES) as soon as no image of it is left to describe.
"""

IMAGES_PENDING = "IMAGES_PENDING"
EMPTY_TEXT = "EMPTY_TEXT"

TERMINAL_STATUSES = (
    "SUCCESS", "SKIPPED_EMPTY_SMALL_FILE", "SKIPPED_REF_MANUAL", "SKIPPED_SOURCE_REMOVED",
    "SKIPPED_EMPTY_IMAGES", "SKIPPED_EMPTY_FOLDER", IMAGES_PENDING,
)

# Statuses 4_describe_images moves on in the same job run.
MOVED_BY_IMAGES = (EMPTY_TEXT, IMAGES_PENDING)

# Image statuses (image_metadata.status). An image still to describe is retried by the next run; the others are final
# for the description step (DONE and SKIPPED are the results, FAILED_LIMIT is reported in the run summary).
IMAGE_RETRIED_STATUSES = ("PENDING", "ERROR")   # rows left with ERROR are retried too
IMAGE_TERMINAL_STATUSES = ("DONE", "SKIPPED", "SKIPPED_DECORATIVE", "EXTRACTION_FAILED", "FAILED_LIMIT")

# Statuses whose derived rows (chunks, image_metadata) are pruned when the document leaves scope.
PRUNED_STATUSES = ("SUCCESS", "ERROR", EMPTY_TEXT, IMAGES_PENDING)
