from pathlib import Path
from itertools import tee
import numpy as np
import torch
import librosa
from base.data.base_dataset import BaseDataset
import json
from math import floor, ceil

class MfccDataset(BaseDataset):

    def __init__(self, opt,receptive_field=None):
        super().__init__()
        self.opt = opt
        self.receptive_field = receptive_field
        data_path = Path(opt.data_dir)
        if not data_path.is_dir():
            raise ValueError(f'Invalid directory: {opt.data_dir}')
        # self.audio_files = sorted(data_path.glob('**/*.ogg'), key=lambda path: path.parent.__str__())
        candidate_audio_files = sorted(data_path.glob('**/*.ogg'), key=lambda path: path.parent.__str__())
        self.level_jsons = []
        self.audio_files = []
        # audio_no_level = []  # store audios for which there is no json level
        for i, path in enumerate(candidate_audio_files):
            try:
                level = list(path.parent.glob(f'./{self.opt.level_diff}.json'))[0]
                self.level_jsons.append(level)
                self.audio_files.append(path)
            except IndexError:
                # audio_no_level.append(i)
                pass
        # for i in reversed(audio_no_level):  # not to throw off preceding indices
        #     self.audio_files.pop(i)
        assert self.audio_files, "List of audio files cannot be empty"
        assert self.level_jsons, "List of level files cannot be empty"
        assert len(self.audio_files) == len(self.level_jsons)
        self.eps = 0.1
        self.mfcc_features = {}

    @staticmethod
    def modify_commandline_options(parser, is_train):
        parser.add_argument('--sampling_rate', default=16000, type=float)
        parser.add_argument('--level_diff', default='Expert', help='Difficulty level for beatsaber level')
        parser.add_argument('--hop_length', default=256, type=int)  # Set the hop length; at 22050 Hz, 512 samples ~= 23ms
        parser.add_argument('--compute_feats', action='store_true', help="Whether to extract musical features from the song")
        parser.add_argument('--padded_length', type=int, default=3000000)
        parser.add_argument('--chunk_length', type=int, default=9000)
        # parser.add_argument('--num_mfcc_features', type=int, default=20)
        # parser.set_defaults(input_channels=(self.opt.num_mfcc_features+(9*3+1)*(4*3)), output_nc=2, direction='AtoB')
        parser.set_defaults(input_channels=(20+12*20))
        parser.set_defaults(num_classes=20)
        parser.set_defaults(output_channels=12)
        return parser

    def name(self):
        return "SongDataset"

    def __getitem__(self, item):
        level = json.load(open(self.level_jsons[item], 'r'))

        bpm = level['_beatsPerMinute']
        notes = level['_notes']

        sr = self.opt.sampling_rate
        beat_duration = int(60*sr/bpm) #beat duration in samples

        mel_hop = beat_duration//self.opt.beat_subdivision #one vec of mfcc features per 16th of a beat (hop is in num of samples)
        mel_window = 4*mel_hop

        if item not in self.mfcc_features: #cache

            y, sr = librosa.load(self.audio_files[item], sr=self.opt.sampling_rate)

            # get mfcc feature
            mfcc = librosa.feature.mfcc(y, sr=sr, hop_length=mel_hop, n_fft=mel_window, n_mfcc=(self.opt.input_channels-self.opt.output_channels*self.opt.num_classes))

            # print(len(y),mel_hop,len(y)/mel_hop,mfcc.shape[1])

            self.mfcc_features[item] = mfcc
        else:
            mfcc = self.mfcc_features[item]

        # y = librosa.util.fix_length(y, size=self.opt.padded_length)
        level = json.load(open(self.level_jsons[item], 'r'))

        bpm = level['_beatsPerMinute']
        features_rate = bpm*self.opt.beat_subdivision
        notes = level['_notes']

        y = mfcc

        receptive_field = self.receptive_field
        output_length = self.opt.output_length
        input_length = receptive_field + output_length -1

        blocks = np.zeros((y.shape[1],12)) #one class per location in the block grid. This still assumes that the classes are independent if we are modeling them as the outputs of a feedforward net
        blocks_manyhot = np.zeros((y.shape[1],self.opt.output_channels,self.opt.num_classes)) #one class per location in the block grid. This still assumes that the classes are independent if we are modeling them as the outputs of a feedforward net
        blocks_manyhot[:,:,0] = 1.0 #default is the "nothing" class
        # eps = self.eps
        for note in notes:
            sample_index = floor((note['_time']*60/bpm)*sr/(mel_hop+1))
            if sample_index > y.shape[1]:
                print("note beyond the end of time")
                continue
            if note["_type"] == 3:
                note_representation = 19
            elif note["_type"] == 0 or note["_type"] == 1:
                note_representation = 1 + note["_type"]*9+note["_cutDirection"]
            else:
                raise ValueError("I thought there was no notes with _type different from 0,1,3. Ahem, what are those??")
            blocks[sample_index,note["_lineLayer"]*4+note["_lineIndex"]] = note_representation
            blocks_manyhot[sample_index,note["_lineLayer"]*4+note["_lineIndex"], 0] = 0.0 #remove the one hot at the zero class
            blocks_manyhot[sample_index,note["_lineLayer"]*4+note["_lineIndex"], note_representation] = 1.0

        indices = np.random.choice(range(y.shape[1]-receptive_field),size=self.opt.num_windows,replace=False)

        input_windows = [y[:,i:i+input_length] for i in indices]
        input_windows = torch.tensor(input_windows)
        input_windows = (input_windows - input_windows.mean())/torch.abs(input_windows).max()

        block_windows = [blocks[i+receptive_field:i+input_length+1,:] for i in indices]
        block_windows = torch.tensor(block_windows,dtype=torch.long)

        blocks_manyhot_windows = [blocks_manyhot[i:i+input_length,:,:] for i in indices]
        blocks_manyhot_windows = torch.tensor(blocks_manyhot_windows)
        blocks_manyhot_windows = blocks_manyhot_windows.permute(0,2,3,1)
        # input_windows = input_windows.permute(0,2,1)
        shape = blocks_manyhot_windows.shape
        blocks_manyhot_windows = blocks_manyhot_windows.view(shape[0],shape[1]*shape[2],shape[3])

        return {'input': torch.cat((input_windows.float(),blocks_manyhot_windows.float()),1), 'target': block_windows}

    def __len__(self):
        return len(self.audio_files)


def pairwise(iterable):
    "s -> (s0,s1), (s1,s2), (s2, s3), ..."
    a, b = tee(iterable)
    next(b, None)
    return zip(a, b)
