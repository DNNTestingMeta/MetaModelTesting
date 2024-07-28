import os

from absl import app
from absl import flags
from datamodels.Cifar.VGG19Model import VGG_Model

import numpy as np
import tensorflow as tf
from selectionmethods import UncertaintyUtils
from keras.datasets import cifar10
from keras.preprocessing.image import ImageDataGenerator
from keras.callbacks import LearningRateScheduler
from keras.callbacks import ReduceLROnPlateau
from keras.utils import np_utils

for name in ('ensemble_size', 'batch_size', 'learning_rate', 'output_dir', 'seed', 'train',
             'nb_epochs', 'l2_reg', 'data_aug', 'validation_freq'):
    if name in flags.FLAGS:
        delattr(flags.FLAGS, name)

flags.DEFINE_integer('ensemble_size', 10, 'Number of ensemble members.')
flags.DEFINE_bool('train', True, 'Whether to train models.')
flags.DEFINE_integer('batch_size', 128, 'Batch size.')
flags.DEFINE_integer('nb_epochs', 120, 'Number of training epochs.')
flags.DEFINE_float('learning_rate', 0.1, 'Learning rate.')
flags.DEFINE_float('l2_reg', 0.0, 'L2 regularization.')
flags.DEFINE_integer('validation_freq', 5, 'Validation frequency in steps.')
flags.DEFINE_string('output_dir', './model/deepensemble/',
                    'The directory where the model weights and '
                    'training/evaluation summaries are stored.')
flags.DEFINE_bool('data_augmentation', True, 'Whether to train with augmented data.')
flags.DEFINE_bool('variational', False, 'Whether to use TFP at last layer')

FLAGS = flags.FLAGS


def generate_VGG_CIFAR_dataset_and_model(ensemble_size=5,
                               train=False, learning_rate=0.001,
                               batch_size=64, validation_freq=5,
                               nb_epoch = 150,
                               output_dir="", l2_reg=0., data_augmentation=True, variational=False):
    seed = 0
    np.random.seed(seed)
    tf.random.set_seed(seed)
    if output_dir == "":
        cwd = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.join(cwd, "model/deepensemble/VGG")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

   # input image dimensions
    img_rows, img_cols = 32, 32

    # The CIFAR10 images are RGB.
    img_channels = 3

    # The data, shuffled and split between train and test sets:
    (x_train, y_train), (x_test, y_test) = cifar10.load_data()
    num_classes = 10

    if not variational:
        y_train = np_utils.to_categorical(y_train, num_classes)
        y_test = np_utils.to_categorical(y_test, num_classes)

    x_train = x_train.astype('float32')
    x_test = x_test.astype('float32')

    x_train /= 255.
    x_test /= 255.

    n_train = x_train.shape[0]


    seed_list = np.arange(ensemble_size)
    ensemble_filenames = []
    for i in range(ensemble_size):

        member_dir = os.path.join(output_dir, 'member_' + str(i))
        if not variational:
            if not data_augmentation:
                filename = "cifarmodel_VGG_SM.h5"
            else:
                filename = "cifarmodel_aug_VGG_SM.h5"
        else:
            if not data_augmentation:
                filename = "cifarmodel_VGG.h5"
            else:
                filename = "cifarmodel_VGG_aug.h5"
        member_filename = os.path.join(member_dir, filename)


        ensemble_filenames.append(member_filename)

        if (train) or ((not train) and (i==0)):

            if not variational:
                model = VGG_Model(input_shape=x_train.shape[1:], learning_rate=learning_rate, batch_norm=True,
                                  prob_last_layer=False)
                sgd = tf.keras.optimizers.SGD(learning_rate=learning_rate, decay=0, momentum=0.9, nesterov=True)
                model.compile(loss='categorical_crossentropy', optimizer=sgd, metrics=['accuracy'])
            else:
                model = VGG_Model(input_shape=x_train.shape[1:], learning_rate=learning_rate, batch_norm=True,
                                  prob_last_layer=True)
                def negative_log_likelihood(y_true, y_pred):
                    return -tf.reduce_mean(y_pred.log_prob(tf.squeeze(y_true)))

                def accuracy(y_true, y_pred):
                    return tf.reduce_mean(tf.cast(tf.math.equal(
                                            tf.math.argmax(input=y_pred.logits, axis=1),
                                            tf.cast(tf.squeeze(y_true), tf.int64)), tf.float32))

                def log_likelihood(y_true, y_pred):
                    return tf.reduce_mean(y_pred.log_prob(tf.squeeze(y_true)))

                def cross_entropy(y_true, y_pred):
                    return tf.reduce_mean(tf.keras.losses.sparse_categorical_crossentropy(tf.squeeze(y_true),
                                                                                  y_pred.logits,
                                                                                  from_logits=True))

                model.compile(
                    optimizer=tf.keras.optimizers.SGD(learning_rate=learning_rate, momentum=0.9),
                    loss=cross_entropy,
                    metrics=[UncertaintyUtils.MeanMetricWrapper(log_likelihood, name='log_likelihood'),
                             UncertaintyUtils.MeanMetricWrapper(accuracy, name='accuracy'),
                             UncertaintyUtils.MeanMetricWrapper(cross_entropy, name='cross_entropy')])

        if train:
            # Prepare callbacks f
            def lr_schedule(epoch):
                return learning_rate * (0.5 ** (epoch // 40))

            lr_scheduler = LearningRateScheduler(lr_schedule)

            lr_reducer = ReduceLROnPlateau(factor=np.sqrt(0.1),
                                           cooldown=0,
                                           patience=5,
                                           min_lr=0.5e-6)

            callbacks = [lr_scheduler]

            if not data_augmentation:
                model.fit(
                    x=x_train,
                    y=y_train,
                    batch_size=batch_size,
                    epochs=nb_epoch,
                    validation_split=0.2,
                    validation_freq=max(
                        (validation_freq * batch_size) // n_train, 1),
                    verbose=1,
                    callbacks=callbacks)
            else:
                print('Using real-time data augmentation.')
                # This will do preprocessing and realtime data augmentation:
                num_train = int(x_train.shape[0] * 0.9)
                num_val = x_train.shape[0] - num_train
                mask = list(range(num_train, num_train + num_val))
                x_val = x_train[mask]
                y_val = y_train[mask]

                mask = list(range(num_train))
                x_train = x_train[mask]
                y_train = y_train[mask]

                datagen = ImageDataGenerator(
                    featurewise_center=False,  # set input mean to 0 over the dataset
                    samplewise_center=False,  # set each sample mean to 0
                    featurewise_std_normalization=False,  # divide inputs by std of the dataset
                    samplewise_std_normalization=False,  # divide each input by its std
                    zca_whitening=False,  # apply ZCA whitening
                    rotation_range=0,  # randomly rotate images in the range (degrees, 0 to 180)
                    width_shift_range=4,  # randomly shift images horizontally (fraction of total width)
                    height_shift_range=4,  # randomly shift images vertically (fraction of total height)
                    horizontal_flip=True,  # randomly flip images
                    vertical_flip=False)  # randomly flip images

                datagen.fit(x_train)

                # Fit the model on the batches generated by datagen.flow().
                model.fit(datagen.flow(x_train, y_train, batch_size=batch_size),
                                    steps_per_epoch=x_train.shape[0] // batch_size,
                                    validation_data=(x_val, y_val),
                                    epochs=nb_epoch, verbose=1,
                                    callbacks=callbacks)

            if not os.path.exists(member_dir):
                os.makedirs(member_dir)

            model.save_weights(member_filename)

    return model, x_test, y_test, ensemble_filenames

