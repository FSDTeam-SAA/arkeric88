import os
import shutil
import stat
import time
import gc

def force_delete_folder(path):
    gc.collect()
    time.sleep(1)

    def remove_readonly(func, file_path, exc_info):
        try:
            os.chmod(file_path, stat.S_IWRITE)
            func(file_path)
        except Exception as e:
            print(f"Failed to delete {file_path}: {e}")

    if os.path.exists(path):
        shutil.rmtree(path, onerror=remove_readonly)
        # print(f"Deleted: {path}")
    else:
        print(f"Path does not exist: {path}")