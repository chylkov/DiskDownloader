"""Skip symlink-specific checks only when Windows denies link creation."""


def symlink_or_skip(case, link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            case.skipTest("Windows requires Developer Mode or symlink creation privileges")
        raise
