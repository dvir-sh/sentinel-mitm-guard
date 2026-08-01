import tensorflow as tf
from tensorflow.keras.applications import MobileNetV2
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import Dense, GlobalAveragePooling2D, Dropout, RandomFlip, RandomRotation, RandomZoom
import matplotlib.pyplot as plt
import numpy as np

BATCH_SIZE = 16
IMG_SIZE = (160, 160)
INITIAL_EPOCHS = 30
DATASET_PATH = 'dataset'
CLASS_NAMES = ['safe_reviewed', 'ad_reviewed']

train_dataset = tf.keras.utils.image_dataset_from_directory(
    DATASET_PATH,
    class_names=CLASS_NAMES,
    validation_split=0.2,
    subset="training",
    seed=123,
    image_size=IMG_SIZE,
    batch_size=BATCH_SIZE
)

validation_dataset = tf.keras.utils.image_dataset_from_directory(
    DATASET_PATH,
    class_names=CLASS_NAMES,
    validation_split=0.2,
    subset="validation",
    seed=123,
    image_size=IMG_SIZE,
    batch_size=BATCH_SIZE
)

AUTOTUNE = tf.data.AUTOTUNE
train_dataset = train_dataset.cache().shuffle(1000).prefetch(buffer_size=AUTOTUNE)
validation_dataset = validation_dataset.cache().prefetch(buffer_size=AUTOTUNE)

data_augmentation = Sequential([
    RandomFlip('horizontal'),
    RandomRotation(0.1),
    RandomZoom(0.1),
])

base_model = MobileNetV2(input_shape=IMG_SIZE + (3,), include_top=False, weights='imagenet')
base_model.trainable = False

model = Sequential([
    tf.keras.Input(shape=IMG_SIZE + (3,)),
    data_augmentation,
    tf.keras.layers.Rescaling(1./127.5, offset=-1),
    base_model,
    GlobalAveragePooling2D(),
    Dropout(0.2),
    Dense(1, activation='sigmoid')
])

model.compile(
    optimizer=tf.keras.optimizers.Adam(learning_rate=0.0001),
    loss=tf.keras.losses.BinaryCrossentropy(),
    metrics=['accuracy']
)

early_stop = tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=5, restore_best_weights=True)

print("Starting Phase 1: Training the Head...")
history = model.fit(
    train_dataset,
    validation_data=validation_dataset,
    epochs=INITIAL_EPOCHS,
    callbacks=[early_stop]
)

print("Starting Phase 2: Fine-Tuning the Base Model...")
base_model.trainable = True

fine_tune_at = 100
for layer in base_model.layers[:fine_tune_at]:
    layer.trainable = False

model.compile(
    optimizer=tf.keras.optimizers.Adam(learning_rate=0.00001),
    loss=tf.keras.losses.BinaryCrossentropy(),
    metrics=['accuracy']
)

FINE_TUNE_EPOCHS = 20
TOTAL_EPOCHS = len(history.epoch) + FINE_TUNE_EPOCHS

history_fine = model.fit(
    train_dataset,
    validation_data=validation_dataset,
    epochs=TOTAL_EPOCHS,
    initial_epoch=history.epoch[-1],
    callbacks=[early_stop]
)

model.save('ad_blocker_model_final.keras')
print("Training complete. Model saved.")

acc = history.history['accuracy'] + history_fine.history['accuracy']
val_acc = history.history['val_accuracy'] + history_fine.history['val_accuracy']

plt.figure(figsize=(8, 8))
plt.subplot(2, 1, 1)
plt.plot(acc, label='Training Accuracy')
plt.plot(val_acc, label='Validation Accuracy')
plt.plot([INITIAL_EPOCHS-1, INITIAL_EPOCHS-1], plt.ylim(), label='Start Fine Tuning')
plt.legend(loc='lower right')
plt.title('Training and Validation Accuracy')
plt.show()


def predict_image(image_path):
    img = tf.keras.utils.load_img(image_path, target_size=IMG_SIZE)
    img_array = tf.keras.utils.img_to_array(img)
    img_array = tf.expand_dims(img_array, 0)
    pred = model.predict(img_array, verbose=0)[0][0]
    label = "AD" if pred > 0.5 else "SAFE"
    print(f"Result for {image_path}: {label} ({pred if pred > 0.5 else 1-pred:.2%})")

# predict_image('test.jpg')
