#!/usr/bin/env bash

set -e

DATA_DIR="LJSpeech-1.1"
TACO_CH=${TACO_CH:-"pretrained_models/tacotron2/nvidia_tacotron2pyt_fp16.pt"}
for FILELIST in ljs_audio_text_train_filelist.txt \
                ljs_audio_text_val_filelist.txt \
                ljs_audio_text_test_filelist.txt \
; do
    python extract_mels.py \
        --dataset-path ${DATA_DIR} \
        --wav-text-filelist filelists/${FILELIST} \
        --batch-size 128 \
        --extract-mels \
        --extract-durations \
        --extract-pitch-char \
        --tacotron2-checkpoint ${TACO_CH}
done
