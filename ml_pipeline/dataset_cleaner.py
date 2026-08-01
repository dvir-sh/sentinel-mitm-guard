import os
import cv2
import shutil


def clean_dataset():
    print("=== DATASET RE-SORTER & CLEANER ===")
    print("1. Process 'ad' folder")
    print("2. Process 'safe' folder")
    print("3. Process both folders")
    choice = input("\nWhich folder do you want to start with? (1/2/3): ")

    dir_ad = "dataset/ad"
    dir_safe = "dataset/safe"

    if choice == '1':
        folders_to_clean = [dir_ad]
    elif choice == '2':
        folders_to_clean = [dir_safe]
    else:
        folders_to_clean = [dir_ad, dir_safe]

    print("\nControls:")
    print(" [a]        : Move to raw 'ad' folder")
    print(" [s]        : Move to raw 'safe' folder")
    print(" [d]        : Delete image")
    print(" [q]        : Quit tool")
    print(" [ANY OTHER]: Mark as reviewed (moves to _reviewed folder)")
    print("=============================\n")

    for folder in folders_to_clean:
        if not os.path.exists(folder):
            print(f"Folder {folder} does not exist. Skipping.")
            continue

        reviewed_folder = folder + "_reviewed"
        os.makedirs(reviewed_folder, exist_ok=True)

        print(f"\nProcessing: {folder}")
        images = [f for f in os.listdir(folder) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]

        if not images:
            print(f"   No images left in {folder}.")
            continue

        for img_name in images:
            img_path = os.path.join(folder, img_name)

            if not os.path.exists(img_path):
                continue

            img = cv2.imread(img_path)
            if img is None:
                print(f"   Could not read {img_name}. Skipping.")
                continue

            h, w = img.shape[:2]
            if h > 800 or w > 800:
                scale = 800 / max(h, w)
                img = cv2.resize(img, (int(w * scale), int(h * scale)))

            window_title = f"Current Folder: {folder.upper()}"
            cv2.imshow(window_title, img)

            key = cv2.waitKey(0) & 0xFF
            cv2.destroyWindow(window_title)

            if key == ord('q'):
                print("Quitting early.")
                cv2.destroyAllWindows()
                return

            elif key == ord('d'):
                os.remove(img_path)
                print(f"   Deleted: {img_name}")

            elif key == ord('a'):
                dest = os.path.join(dir_ad, img_name)
                if img_path != dest:
                    shutil.move(img_path, dest)
                    print(f"   Moved to raw ad: {img_name}")
                else:
                    print(f"   Image is already in ad folder.")

            elif key == ord('s'):
                dest = os.path.join(dir_safe, img_name)
                if img_path != dest:
                    shutil.move(img_path, dest)
                    print(f"   Moved to raw safe: {img_name}")
                else:
                    print(f"   Image is already in safe folder.")

            else:
                shutil.move(img_path, os.path.join(reviewed_folder, img_name))
                print(f"   Reviewed: {img_name}")

    cv2.destroyAllWindows()
    print("\nDone.")


if __name__ == "__main__":
    clean_dataset()
