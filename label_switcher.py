import argparse
import io
import numpy as np
import os
import pandas as pd
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import qrcode
import struct
import sys
from .utils.constants import FORMAT_CHARACTERS, TAGNAMES, TYPE_DICT, COMPRESSION
from .utils.tiffwriter import BigTiffMaker, LabelSaver


class BigTiffFile():
    def __init__(self, file_path) -> None:
        """Reads BigTiff file header and IFD information. The information can be printed for
        informational purposes. Can be used in isolation with de_identify_slide to overwrite 
        the label and macro images in SVS files.

        Args:
            file_path (str | BytesIO): file path as a string or image as a BytesIO object
        """
        self.file_path = file_path
        self.tiff_info = {}
        self.next_dir_offsets = {}
        self.directory_offsets = {}
        self.directory_count = 0

        self._label = None
        self._macro = None

        #TODO add classic tiff support
        self.endian = None
        self.bigtiff = False

        if isinstance(file_path, io.BytesIO):
            bigtiff = file_path
            next_offset = self._read_header(bigtiff)
            while next_offset != 0:  
                next_offset = self._read_IFDs(bigtiff, next_offset)
        else:
            with open(file_path, 'rb') as bigtiff:
                next_offset = self._read_header(bigtiff)
                while next_offset != 0:  
                    next_offset = self._read_IFDs(bigtiff, next_offset)  
                self._get_label_and_macro_info()


    def de_identify_slide(self):
        """Overwrites the macro and label data with 0s.
        """
        label_strip_offset = self._label['strip offset']
        label_byte_count = self._label['strip byte counts']
        macro_strip_offset = self._macro['strip offset']
        macro_byte_count = self._macro['strip byte counts']
        
        if 'DigitalPathology' in str(self.file_path):
            raise RuntimeError('Cannot remove labels in provided directory!!')
        
        with open(self.file_path, 'rb+') as tiff:
            tiff.seek(label_strip_offset)
            tiff.write(b'\0' * label_byte_count)
            tiff.seek(macro_strip_offset)
            tiff.write(b'\0' * macro_byte_count)


    def get_label(self):
        """Returns the label image as a Pillow Image object
        """
        ls = LabelSaver()
        img = ls.label(self.label_data, self.label_info)
        return img

    def print_IFDs(self, writer=sys.stdout):
        writer.write('=' * 80 + '\n')
        writer.write('=' * 80 + '\n')
        for directory, ifds in self.tiff_info.items():
            writer.write('*' * 80 + '\n')
            writer.write(f'DIRECTORY:\t{directory}\t\t Offset: {self.directory_offsets[directory]}' + '\n')
            for ifd_tag, ifd_data in ifds.items():
                writer.write('_' * 80 + '\n')
                writer.write('IFD Offset: {}\n'.format(ifd_data.get('pre_tag_offset')))
                writer.write('IFD Tag:\t{}\t{}'.format(ifd_tag, TAGNAMES.get(ifd_tag)) + '\n')
                writer.write('IFD Type:\t{}\t{}'.format(ifd_data.get('ifd_type'), TYPE_DICT.get(ifd_data.get('ifd_type'))) + '\n')
                writer.write('IFD Count:\t{}'.format(ifd_data.get('ifd_count')) + '\n')
                writer.write('Data Offset:\t{}'.format(ifd_data.get('data_offset')) + '\n')
                writer.write('Value:\t\t{}'.format(ifd_data.get('value')) + '\n')
            writer.write('Next Directory Offset: {}\n'.format(self.next_dir_offsets[directory]['next_ifd_offset']))
            writer.write('\n')

    def _read_header(self, bigtiff):
        endian = bigtiff.read(2).decode('UTF-8')
        version = struct.unpack('<H', bigtiff.read(struct.calcsize('H')))[0]
        offset_size, reserved = struct.unpack('<HH', bigtiff.read(struct.calcsize('HH')))
        initial_offset = struct.unpack('<Q', bigtiff.read(struct.calcsize('Q')))[0]
        if endian != 'II' or version != 43 or offset_size != 8 or reserved != 0:
            _error = 'File Not Supported: {}\nEndian: {}\nVersion: {}\nOffset_size: {}\nReserved: {}'.format(
                self.file_path,
                endian,
                version,
                offset_size,
                reserved
                )
            raise Exception(_error)    
        return initial_offset
        
    def _read_IFDs(self, bigtiff, directory_offset):
        self.directory_count += 1
        bigtiff.seek(directory_offset)
        IFD_info = {}
        num_of_entries = struct.unpack('<Q', bigtiff.read(8))[0]
        for _ in range(num_of_entries):
            tag_offset = bigtiff.tell()
            IFD_tag, IFD_type, IFD_count = struct.unpack('<HHQ', bigtiff.read(struct.calcsize('<HHQ')))
            pre_data_offset = bigtiff.tell()
            data_offset = struct.unpack('<Q', bigtiff.read(struct.calcsize('<Q')))[0]

            IFD_info[IFD_tag] = {
                'pre_tag_offset': tag_offset,
                'ifd_type': IFD_type,
                'ifd_count': IFD_count,
                'pre_data_offset': pre_data_offset,
                'data_offset': data_offset,
                'value': self._ifd_value(IFD_tag, IFD_type, IFD_count, pre_data_offset, data_offset, bigtiff)
            }
        # position before the next IFD offset. This can be used to change
        # the location of the next IFD
        offset_before_next_ifd_offset = bigtiff.tell()
        next_ifd_offset = struct.unpack('<Q', bigtiff.read(8))[0]
        self.tiff_info[self.directory_count] = IFD_info
        self.directory_offsets[self.directory_count] = directory_offset
        self.next_dir_offsets[self.directory_count] = {
            'pre_offset_offset': offset_before_next_ifd_offset,
            'next_ifd_offset': next_ifd_offset,
            'directory_offset': directory_offset
        }
        return next_ifd_offset
    

    def _ifd_value(self, ifd_tag, ifd_type, ifd_count, pre_data_offset, data_offset, bigtiff):
        fmt = '<' + str(ifd_count) + FORMAT_CHARACTERS[ifd_type]
        length = struct.calcsize(fmt)
        if length <= struct.calcsize('Q'):
            start = bigtiff.tell()
            bigtiff.seek(pre_data_offset)
            value = struct.unpack(fmt, bigtiff.read(struct.calcsize(fmt)))
            bigtiff.seek(start)
        elif ifd_tag in [270, 258]:
            current_position = bigtiff.tell()
            bigtiff.seek(data_offset)
            value = struct.unpack(fmt, bigtiff.read(struct.calcsize(fmt)))
            if TYPE_DICT.get(ifd_type) == 'ASCII':
                value = b''.join(value)
            bigtiff.seek(current_position)
        else:
            return 'Too long to display'
        return value

    def _get_label_and_macro_info(self):
        #the label is the second to last directory, compressed with LZW, and may (depending
        #on Leica software version) have label in tag 270
        proposed_label_directory = len(self.tiff_info.items()) - 1
        proprosed_macro_directory = len(self.tiff_info.items())

        label_compression = self.tiff_info[proposed_label_directory][259]['data_offset']
        try:
            image_description = self.tiff_info[proposed_label_directory][270]['value']
        except Exception:
            image_description = None

        if COMPRESSION.get(label_compression) == 'LZW' or b'label' in image_description or b'Label' in image_description:
            self._label = {
                'label directory': proposed_label_directory,
                'label ifd info': self.tiff_info[proposed_label_directory],
                'strip offset': self.tiff_info[proposed_label_directory][273]['data_offset'],
                'strip byte counts': self.tiff_info[proposed_label_directory][279]['data_offset']
            }

        macro_compression = self.tiff_info[proprosed_macro_directory][259]['data_offset']
        try:
            image_description = self.tiff_info[proprosed_macro_directory][270]['value']
        except Exception:
            image_description = None

        if COMPRESSION.get(macro_compression) in ['JPEG', 'JPEG 7'] or b'macro' in image_description or b'Macro' in image_description:
            self._macro = {
                'macro directory': proprosed_macro_directory,
                'macro ifd info': self.tiff_info[proprosed_macro_directory],
                'strip offset': self.tiff_info[proprosed_macro_directory][273]['data_offset'],
                'strip byte counts': self.tiff_info[proprosed_macro_directory][279]['data_offset']
            }

    @property
    def label_IFD_offset_adjustment(self):
        """The offset of the label directory

        Returns:
            int: offset of IFD for the label
        """
        offset = self.directory_offsets[self._label['label directory']]
        return offset
    

    def _get_label_data(self):
        label_strip_offset = self._label['strip offset']
        label_byte_count = self._label['strip byte counts']

        with open(self.file_path, 'rb') as tiff:
            tiff.seek(label_strip_offset)
            label_data = tiff.read(label_byte_count)
        return label_data

    @property
    def label_data(self):
        """Label data in bytes. Does not include the IFD. Must be used
        before overwriting the label with de_identify_slide

        Returns:
            bytes: byte string containing the raw label information
        """
        return self._get_label_data()

    @property
    def label_info(self):
        """Information on the label BigTiff directory. Used in the LabelSaver
        under TiffWriter to save the label on SVS GT450 v1.0.0 slides that openslide
        cannot find.

        Returns:
            dict: label IFD info
        """
        return self._label

class SubImage():
    def __init__(self, file_type, label_params=None) -> None:
        """Creates a label or macro image to write into a whole slide image. Only
        works with Aperio whole slide images

        Args:
            file_type (str): Must be 'label' or 'macro'
            temp_dir (str): directory to store the saved label and macro images
            label_params (dict, optional): Contains the text for the QR code and any desired
            sub text beneath the QR code. Supports ~ 3 lines of text. More may not fit on 
            the label. Defaults to None.

        Raises:
            ValueError: If file type is not 'label' or 'macro'
        """
        self.label_params = label_params

        if file_type not in ['label', 'macro']:
            raise ValueError(f'{file_type} must be label or macro')
        self.file_type = file_type
        self.file_name = None
        
        self._label_offset_adjustment = None
    
    def create_image(self):
        if self.file_type == 'label':
            img = self._create_label()

        else:
            img = self._create_macro()

        img = np.array(img)

        if self.file_type == 'label':
            btm = BigTiffMaker(img, 'label')
            img = btm.create_image()

        else:
            btm = BigTiffMaker(img, 'macro')
            img = btm.create_image()
        
        return img
    
    def _create_macro(self, img_dims=(1495, 606)):
        img = Image.new('RGB', img_dims, 'red')
        return img
        
    def _create_label(self, img_dims =(609, 567)):
        """Creates a label image with a QR code and text under the QR code

        Returns:
            img: label with image
        """
        

        try:
            myFont = ImageFont.truetype('arial.ttf', size=30) # Windows
        except OSError:
            try:
                myFont = ImageFont.truetype('Arial.ttf', size=30) # Mac
            except OSError:
                    print('FONT NOT FOUND ERROR')
                    sys.exit()

        qr_img = None
        if self.label_params: # qr code string
            qr_data = self.label_params[0]
            if qr_data is not None:
                qr_img = qrcode.make(qr_data)
                width, height = qr_img.size
                if width < img_dims[0] or height < img_dims[0]:
                    width, height = img_dims
        else:
            width, height = img_dims
                
        if qr_img:
            img_dims = (int(width *1.5), int(height *1.5))
    
        img = Image.new('RGB', img_dims, 'white')
        ruo_text = 'RUO'
        img_draw = ImageDraw.Draw(img)
        img_draw.text((img_dims[0]-150, 10), text=ruo_text, font=myFont, fill=(0, 0, 0))

        if qr_img:
            img.paste(qr_img)

        if self.label_params:
            for line_num, text in enumerate(self.label_params[1:]):
                y_offset = 60
                img_draw = ImageDraw.Draw(img)

                if text:
                    if not isinstance(text, str):
                        text = str(text)
                    
                    y_coord = height + y_offset * line_num # 380 is the distance the text is displaced below the qrcode
                    img_draw.text((28, y_coord), text, font=myFont, fill=(0, 0, 0))

        return img
        

    def update_ifd(self, file, offset_adjustment):
        """Updates the SVS file IFDs for the label and macro to correct the offsets.

        Args:
            file (BytesIO): BytesIO image object
            offset_adjustment (int): offset to adjust the IFD

        Returns:
            BytesIO: Label or macro image file with updated IFDs to be inserted into the SVS file
        """
        tiff_data = BigTiffFile(file)
        dir_offsets = tiff_data.next_dir_offsets

        for tag in tiff_data.tiff_info[1].keys():
            ifd_count = tiff_data.tiff_info[1][tag]['ifd_count']
            ifd_type = tiff_data.tiff_info[1][tag]['ifd_type']

            fmt = '<' + str(ifd_count) + FORMAT_CHARACTERS[ifd_type]
            length = struct.calcsize(fmt)
                
            if length > struct.calcsize('<Q') or tag == 273:
                pre_data_offset = tiff_data.tiff_info[1][tag]['pre_data_offset']
                data_offset = tiff_data.tiff_info[1][tag]['data_offset']

                new_offset = data_offset + offset_adjustment - 16                    

                updated_offset = struct.pack('<Q', new_offset)
                file.seek(pre_data_offset)
                file.write(updated_offset)

        # updates the next IFD of the label directory
        if self.file_type == 'label':
            end_of_ifd = dir_offsets[1]['pre_offset_offset']
            file.seek(0, os.SEEK_END)
            end_of_file = file.tell()
            new_next_ifd_offset = end_of_file + offset_adjustment
            new_next_ifd = struct.pack('<Q', new_next_ifd_offset)
            file.seek(end_of_ifd)
            file.write(new_next_ifd)
            self._label_offset_adjustment = new_next_ifd_offset
        
        return file

    @property
    def offset_adjustment(self):
        return self._label_offset_adjustment


class LabelSwitcher():
    def __init__(self, slide_path, remove_original_label_and_macro: bool=True, \
        qrcode:str=None, text_line1:str=None, text_line2:str=None, text_line3:str=None, text_line4:str=None) -> None:
        """WARNING: THIS UTILITY PERFORMS IN PLACE OPERATIONS ON SVS FILES. THE FILES ARE NOT COPIED!
        PLEASE MAKE COPIES PRIOR TO USE.

        Primary utility to switch the SVS label with a custom QR code and up to 3 lines of text.
        By default, the original label and macro images are overwritten with 0s. Because the slides are not
        copied, the process is fast.

        Caveats:
            1. Only tested on GT450 V1.0.0 and V1.0.1 SVS files
            2. Will corrupt files if unexpected data is present or process is terminated early
            3. Unknown performance if label or macro have been previously removed with another program

        Args:
            slide_path (str): full path to SVS file
            remove_original_label_and_macro (bool, optional): flag True to overwrite the original label and macro. Defaults to True.
            text_line1 (str, optional): line of text that appears on label. Defaults to None.
            text_line2 (str, optional): line of text that appears on label. Defaults to None.
            text_line3 (str, optional): line of text that appears on label. Defaults to None.
        """

        self.slide_path = slide_path
        label_params=[qrcode, text_line1, text_line2, text_line3, text_line4]
        self._slide_offset_adjustment = self._get_slide_offset(remove_original_label_and_macro)
        self._next_ifd_offset_adjustment, self._label_img = self._get_label_img(label_params)
        self._macro_img = self._get_macro_img()
    
    def switch_labels(self):
        with open(self.slide_path, 'rb+') as slide:
            
            slide.seek(self._slide_offset_adjustment)

            self._label_img.seek(16)
            label_data = self._label_img.read()
            slide.write(label_data)

            self._macro_img.seek(16)
            macro_data = self._macro_img.read()
            slide.seek(self._next_ifd_offset_adjustment)
            slide.write(macro_data)

    def _get_slide_offset(self, remove_label_and_macro):
        slide = BigTiffFile(self.slide_path)
        if remove_label_and_macro:
            slide.de_identify_slide()
        return slide.label_IFD_offset_adjustment

    def _get_label_img(self, label_params):
        img_creator = SubImage('label', label_params)
        label_image = img_creator.create_image()
        label_image = img_creator.update_ifd(label_image, self._slide_offset_adjustment)

        next_ifd_offset = img_creator.offset_adjustment
        return next_ifd_offset, label_image
    
    def _get_macro_img(self):
        img_creator = SubImage('macro')
        macro_image = img_creator.create_image()
        macro_image = img_creator.update_ifd(macro_image, self._next_ifd_offset_adjustment)
        return macro_image
    

def switch_labels_from_file(file_path: str, col_with_slide_names: str, slide_dir: str=None):
    """THIS IS A DESTRUCTIVE PROCESS - MAKE COPIES FIRST! Deletes the original label and macro image 
    on a slide and replaces the label with a custom label containing a QR code 
    and up to 3 lines of text. The CSV file must include at least a 'File Location' 
    header. The QR data should be under the header 'QR'. Text should be placed under: 
    'text1', 'text2', and 'text3'. If the QR or text headers are not present,
    Placeholder Text will be used for the QR code and blank lines for the text.

    Args:
        file_path (str): path to csv files containing appropriate headers
    """
    if Path(file_path).suffix == '.xlsx':
        df = pd.read_excel(file_path)
    elif Path(file_path).suffix =='.csv':
        df = pd.read_csv(file_path)
    else:
        raise Exception('Only accepts csv and xlsx files')

    for index, row in df.iterrows():
        slide = Path(row[col_with_slide_names])

        if slide_dir is not None:
            if slide.suffix != '.svs':
                slide_name = str(slide.name) + '.svs'
            else:
                slide_name = slide.name

            slide_path = Path(slide_dir).joinpath(slide_name)
        else:
            slide_path = Path(slide)

        if 'DigitalPathology' in str(slide_path):
            raise RuntimeError('Cannot remove labels in provided directory!!')
        
        try:
            qr_data = row['QR']
        except KeyError:
            qr_data = None
        text_dict = {}
        expected_text_headers = ['line1', 'line2', 'line3', 'line4']
        for text_head in expected_text_headers:
            try:
                text1 = row[text_head]
                if len(text1) >= 60:
                    print(f'Warning: "{text1}" may not fit on label - Recommended string length is 60 - current string is {len(text1)}\n')
                text_dict[text_head] = text1
                
            except KeyError:
                text_dict[text_head] = None
        
        if int(Path(slide_path).stem[:5]) < 563:
            continue

        try:

            label_switcher = LabelSwitcher(
                slide_path=slide_path,
                remove_original_label_and_macro=True,
                qrcode=qr_data,
                text_line1=text_dict.get('line1'),
                text_line2=text_dict.get('line2'),
                text_line3=text_dict.get('line3'),
                text_line4=text_dict.get('line4'))

            label_switcher.switch_labels()
        except Exception as e:
            print('*' * 50, '\n', e, '\n', '*' * 50)


def label_saver(args: argparse.Namespace):
    path = args.path
    output_directory = args.outdir

    if Path(path).is_dir():
        slides = Path(path).glob('*.svs')
    elif Path(path).is_file() and Path(path).suffix == '.svs':
        slides = [path]
    else:
        _error = f'{path} is not a valid file or directory'
        raise ValueError(_error)
        
    for slide in slides:
        save_name = Path(output_directory).joinpath(slide.stem + '.jpg')
        try:
            label = BigTiffFile(slide)
            img = label.get_label()
            img.save(save_name)
        except Exception as e:
            print(e)

def single_slide_switch_labels(args: argparse.Namespace):
    label_switcher = LabelSwitcher(
        slide_path=args.p,
        remove_original_label_and_macro=True,
        qrcode=args.qr,
        text_line1=args.l1,
        text_line2=args.l1,
        text_line3=args.l1,
        text_line4=args.l1)

    label_switcher.switch_labels()


def multiple_slide_switch_labels(args: argparse.Namespace):
    switch_labels_from_file(
        file_path=args.p,
        col_with_slide_names=args.hd,
        slide_dir=args.dir
    )



if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='SVS Label Switcher', description='''Replaces labels and deletes images from SVS files - only tested on Leica Aperio GT450 v1.0.0 and v1.0.1
    Supports up to 3 lines of text on a label. The headers for each line should be "text1", "text2", "text3". The QR code column should be labeled "QR"'''
    )

    subparsers = parser.add_subparsers(
        title='Label Switcher', 
        description='"Single" is useful to change the label on one file; "Multiple" uses a csv or xlsx file for batch swapping',
        metavar='Commands'
        )
    
    
    single = subparsers.add_parser(
        'single', 
        help='switch the label on a single svs file'
        )
    single.add_argument('-p',help='Path to slide', required=True, metavar='path')
    single.add_argument('-qr', help='QR code text', default=None)
    single.add_argument('-l1', help='Line 1 text', default=None, metavar='Line 1')
    single.add_argument('-l2', help='Line 2 text', default=None, metavar='Line 2')
    single.add_argument('-l3', help='Line 3 text', default=None, metavar='Line 3')
    single.add_argument('-l4', help='Line 4 text', default=None, metavar='Line 4')
    single.set_defaults(func=single_slide_switch_labels)


    multiple = subparsers.add_parser(
        'multiple', 
        help='Switch labels on multiple files using a csv or xlsx file'
        )
    multiple.add_argument(
        '-p', 
        help='path to csv or xlsx file containing list of slides', 
        required=True
        )
    multiple.add_argument(
    '-hd',
    help='column header that contains the slide names or full paths (with or without extensions)',
    default='File Location'
        )
    multiple.add_argument(
        '-dir', 
        help='path to slide directory - optional (useful if files have switched directories, but names have not)', 
        default=None
        )
    
    multiple.set_defaults(func=multiple_slide_switch_labels)


    save_label = subparsers.add_parser(
        'label', 
        help='Save labels from all slides in one directory to specified directory'
        )
    save_label.add_argument(
        '-path', 
        help='Path to SVS file or directory containing SVS files in BigTiff format', 
        required=True
        )
    save_label.add_argument(
        '-outdir', 
        help='Output directory to save label(s)', 
        required=True
        )
    save_label.set_defaults(func=label_saver)


    args = parser.parse_args()
    args.func(args)

