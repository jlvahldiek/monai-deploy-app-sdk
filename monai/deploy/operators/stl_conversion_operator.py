# Copyright 2022 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Optional

import nibabel as nib
import numpy as np

from monai.deploy.utils.importutil import optional_import

sitk, _ = optional_import("SimpleITK")
label, _ = optional_import("skimage.measure", name="label")
measure, _ = optional_import("skimage", name="measure")
mesh, _ = optional_import("stl", name="mesh")
resize, _ = optional_import("skimage.transform", name="resize")
trimesh, _ = optional_import("trimesh")

import monai.deploy.core as md
from monai.deploy.core import DataPath, ExecutionContext, Image, InputContext, IOType, Operator, OutputContext

__all__ = ["STLConversionOperator", "STLConverter"]


@md.input("image", Image, IOType.IN_MEMORY)
@md.output("stl_output", DataPath, IOType.DISK)
@md.env(pip_packages=["numpy>=1.21", "numpy-stl>=2.12.0", "scikit-image>=0.17.2", "trimesh>=3.8.11"])
class STLConversionOperator(Operator):
    """Converts volumetric image to surface mesh in STL format, file output only."""

    def __init__(
        self, output_file=None, class_id=None, is_smooth=True, keep_largest_connected_component=True, *args, **kwargs
    ):
        """Creates an object to generate a surface mesh and saves it as an STL file if the path is provided.

        Args:
            output_file (str, optional): output STL file relative path. Default to None for no file output.
            class_id (array, optional): Class label ids. Defaults to None.
            is_smooth (bool, optional): smoothing or not. Defaults to True.
            keep_largest_connected_component (bool, optional): Defaults to True.
        """
        super().__init__(*args, **kwargs)
        self._logger = logging.getLogger("{}.{}".format(__name__, type(self).__name__))
        self._class_id = class_id
        self._is_smooth = is_smooth
        self._keep_largest_connected_component = keep_largest_connected_component
        self._output_file = output_file if output_file and len(output_file) > 0 else None

        self._converter = STLConverter(*args, **kwargs)

    def compute(self, op_input: InputContext, op_output: OutputContext, context: ExecutionContext):
        """Gets the input (image), processes it and sets results in the output.

        When used in a leaf operator, this function cannot set its output as in-memory object due to
        current limitation, and only file output, for DataPath IOType_DISK, will be saved in the
        op_output path, which is mapped to the application's output path by the execution engine.

        Args:
            op_input (InputContext): An input context for the operator.
            op_output (OutputContext): An output context for the operator.
            context (ExecutionContext): An execution context for the operator.
        """

        input_image = op_input.get("image")
        if not input_image:
            raise ValueError("Input is None.")

        op_output_config = op_output.get()
        if self._output_file and len(self._output_file) > 0:
            # The file output folder is either the op_output or app's output depending on output types.
            output_folder = (
                op_output_config.path if isinstance(op_output_config, DataPath) else context.output.get().path
            )
            self._output_file = output_folder / self._output_file
            self._output_file.parent.mkdir(exist_ok=True)
            self._logger.info(f"Output will be saved in file {self._output_file}.")

        stl_bytes = self._convert(input_image, self._output_file)

        if not isinstance(op_output_config, DataPath):
            op_output.set(stl_bytes)

    def _convert(self, image: Image, output_file: Optional[Path] = None):
        """
        Args:
            image (Image): object with the image (ndarray in DHW) and its metadata dictionary.
            output_file (Path, optional): output file path. Default None for no file output.

        Returns:
            Bytes: Bytes of the binary of STL file
        """

        # Use path in the output_file arg if provided.
        if isinstance(output_file, Path):
            output_file.parent.mkdir(exist_ok=True)
        else:
            output_file = self._output_file

        return self._converter.convert(
            image=image,
            output_file=output_file,
            class_ids=self._class_id,
            is_smooth=self._is_smooth,
            keep_largest_connected_component=self._keep_largest_connected_component,
        )


class STLConverter(object):
    """Converts volumetric image to surface mesh in STL"""

    def __init__(self, *args, **kwargs):
        """Creates an instance to generate a surface mesh in STL with an Image object."""

        super().__init__(*args, **kwargs)
        self._logger = logging.getLogger("{}.{}".format(__name__, type(self).__name__))

    def convert(
        self,
        image: Image,
        output_file: Optional[Path] = None,
        class_ids=None,
        is_smooth=True,
        keep_largest_connected_component=True,
    ):
        """
        Args:
            image (Image): object with the image (ndarray of DHW index order) and its metadata dictionary.
            output_file (str): output STL file path. Default to None for not saving output file.
            class_id (array, optional): Class label id. Defaults to None.
            is_smooth (bool, optional): smoothing or not. Defaults to True.
            keep_largest_connected_component (bool, optional): Defaults to True.

        Returns:
            Bytes of the binary STL file.
        """

        if not image or not isinstance(image, Image):
            raise ValueError("image is not a Image object.")

        if not isinstance(output_file, Path):
            raise ValueError("output_file is not a Path")

        # Ensure output file's folder exists
        if output_file.parent:
            output_file.parent.mkdir(exist_ok=True)

        s_image = self.SpatialImage(image)
        nda = s_image.image_array
        self._logger.info(f"Image ndarray shape:{nda.shape}")

        if keep_largest_connected_component:
            nda = STLConverter.get_largest_cc(nda)

        res = s_image.spacing
        if res is None:
            raise ValueError("Image spacing/resolution is missing.")

        # In case image has been re-oriented from the original
        affine = s_image.original_affine
        if (
            affine is not None
            and s_image.affine is not None
            and np.sum(np.abs(s_image.original_affine - s_image.affine)) > 1e-7
        ):
            codes = nib.orientations.axcodes2ornt(nib.orientations.aff2axcodes(np.linalg.inv(affine)))
            nda = nib.orientations.apply_orientation(np.squeeze(nda), codes)

        new_nda = np.zeros(shape=nda.shape, dtype=np.uint8)
        if class_ids is None:
            new_nda += (nda > 0).astype(np.uint8)
        elif isinstance(class_ids, list):
            for class_id in class_ids:
                new_nda += (nda == class_id).astype(np.uint8)
        else:
            try:
                new_nda += (nda == class_ids).astype(np.uint8)
            except ValueError as err:
                err_msg = "That was no valid value for class_id."
                self._logger.error(err_msg)
                raise ValueError(err_msg) from err

        max_res = np.amin(res)
        target_shape = []
        for _j in range(3):
            length = float(nda.shape[_j]) * res[_j] / max_res
            length = int(np.round(length))
            target_shape.append(length)

        new_nda = STLConverter.resize_volume(nda, output_shape=target_shape)

        verts, faces, _, _ = measure.marching_cubes(new_nda, level=0.5, step_size=5)

        for _j in range(3):
            verts[:, _j] = (verts[:, _j] + 0.5) * float(nda.shape[_j]) / float(new_nda.shape[_j]) - 0.5

        itk_image = s_image.itk_image
        for _j in range(verts.shape[0]):
            vert = (float(verts[_j, 0]), float(verts[_j, 1]), float(verts[_j, 2]))
            vert = itk_image.TransformContinuousIndexToPhysicalPoint(vert)
            verts[_j, :] = np.array(vert)

        # Write out the STL file, and then load into trimesh
        try:
            temp_folder = tempfile.mkdtemp()
            raw_stl_filename = os.path.join(temp_folder, "temp.stl")
            STLConverter.write_stl(verts, faces, raw_stl_filename)
            mesh_data = trimesh.load(raw_stl_filename)

            if is_smooth:
                trimesh.smoothing.filter_taubin(mesh_data, iterations=20)

            final_file_path = output_file if output_file else os.path.join(temp_folder, "surface_mesh.stl")
            mesh_data.export(final_file_path)
            with open(str(final_file_path), "rb") as r_file:
                stl_bytes = r_file.read()
        finally:
            shutil.rmtree(temp_folder)

        return stl_bytes

    # Helper functions
    @staticmethod
    def get_largest_cc(nda):
        logging.debug("ndarray shape: {}".format(nda.shape))
        labels = label(nda)

        # assume at least 1 CC
        assert labels.max() != 0
        largest_cc = labels == np.argmax(np.bincount(labels.flat)[1:]) + 1
        largest_cc = largest_cc.astype(np.uint8)
        return largest_cc

    @staticmethod
    def resize_volume(nda, output_shape, order=1, preserve_range=True, anti_aliasing=False):
        return resize(
            nda, output_shape, order=order, mode="constant", preserve_range=preserve_range, anti_aliasing=anti_aliasing
        )

    @staticmethod
    def write_stl(verts, faces, filename):
        # Create the mesh
        cube = mesh.Mesh(np.zeros(faces.shape[0], dtype=mesh.Mesh.dtype))
        for i, f in enumerate(faces):
            for j in range(3):
                cube.vectors[i][j] = verts[f[j], :]

        cube.save(os.path.splitext(filename)[0] + ".stl")

    # Helper class for wrapping the App SDK Image object
    #
    class SpatialImage:
        """Object encapsulating a spatial volume image instance of Image.

        Channel is not supported in this version.
        """

        def __init__(self, image: Image, dtype=np.float32):
            """Creates an instance.

            Args:
                image(Image): An instance of Image.
                dtype (Numpy type, optional): Defaults to np.float32.
            """

            self._logger = logging.getLogger("{}.{}".format(__name__, type(self).__name__))
            if not image or not isinstance(image, Image):
                raise ValueError("Argument is not a Image object.")
            self._image = image
            self._dtype = dtype

            self._props: Dict = {}
            """ Properties may include some or all of the following
                img_array
                shape
                spacing
                original_affine
                affine
            """
            self._read_from_in_mem_image(self._image)

        @property
        def image_array(self):
            """Image data in Numpy array, or None"""
            return self._props.get("img_array", None)

        @property
        def itk_image(self):
            """ITK image object created from the encapsulated image object, or None"""
            return self._props.get("itk_image", None)

        @property
        def shape(self):
            """Shape of image array, or None"""
            return self._props.get("shape", None)

        @property
        def spacing(self):
            """Pixel spacing of original image, aka resolution, or None"""
            return self._props.get("spacing", None)

        @property
        def original_affine(self):
            """Original affine of the image, or None"""
            return self._props.get("original_affine", None)

        @property
        def affine(self):
            """Affine of the re-oriented image data, or None"""
            return self._props.get("affine", None)

        def set_property(self, key: str, value):
            """Sets an image property

            Args:
                key (str): key of the property
                value: value of the property
            """
            self._props[key] = value

        def get_property(self, key: str, default=None):
            """Gets value of the specified property

            Args:
                key (str): key of the property
                default: default value if the property does not exist.

            Returns:
                the value of the property, or the default value if property does not exist.

            """
            return self._props.get(key, default)

        def get_data(self):
            """Returns the image array in ndarray"""
            return self._props.get("img_array", None)

        def _load_data(self, image):
            img_array = image.asnumpy()
            img_meta_dict = image.metadata()
            shape = np.asarray(image.asnumpy().shape)
            spacing = np.asarray(
                (
                    img_meta_dict["row_pixel_spacing"],
                    img_meta_dict["col_pixel_spacing"],
                    img_meta_dict["depth_pixel_spacing"],
                )
            )
            original_affine = img_meta_dict["nifti_affine_transform"]
            affine = original_affine
            itk_image = sitk.GetImageFromArray(img_array)
            itk_image.SetSpacing(
                [
                    float(img_meta_dict["row_pixel_spacing"]),
                    float(img_meta_dict["col_pixel_spacing"]),
                    float(img_meta_dict["depth_pixel_spacing"]),
                ]
            )

            direction = []
            direction.extend(img_meta_dict["row_direction_cosine"])
            direction.extend(img_meta_dict["col_direction_cosine"])
            direction.extend(img_meta_dict["depth_direction_cosine"])
            itk_image.SetDirection(direction)

            return img_array, affine, original_affine, shape, spacing, itk_image

        def _read_from_in_mem_image(self, image):
            """Parse the in-memory image for the attributes.

            Args:
                image (Image): App SDK Image instance.

            Returns:
                An instance of SpacialImage.
            """
            img_array, affine, original_affine, shape, spacing, itk_image = self._load_data(image)
            num_dims = len(img_array.shape)
            img_array = img_array.astype(self._dtype)

            if num_dims == 2:
                self._logger.info("2D image")
            elif num_dims == 3:
                self._logger.info("3D image")
            elif num_dims <= 5:
                # if 4d data, we assume 4th dimension is channels.
                # if 5d data, try to squeeze 5th dimension.
                if num_dims == 5:
                    img_array = np.squeeze(img_array)
                    if len(img_array.shape) != 4:
                        raise ValueError("Cannot squeeze 5D image to 4D; object doesn't support time based data.")

                if self.is_channels_first:
                    self._logger.info("4D image, channel first")
                else:
                    self._logger.info("4D image, channel last")
            else:
                raise NotImplementedError("Object does not support image of dims {}".format(num_dims))

            self._props["original_affine"] = original_affine
            self._props["affine"] = affine
            self._props["spacing"] = spacing
            self._props["shape"] = shape
            self._props["img_array"] = img_array
            self._props["itk_image"] = itk_image


def test():
    from monai.deploy.operators.dicom_data_loader_operator import DICOMDataLoaderOperator
    from monai.deploy.operators.dicom_series_selector_operator import DICOMSeriesSelectorOperator
    from monai.deploy.operators.dicom_series_to_volume_operator import DICOMSeriesToVolumeOperator

    current_file_dir = Path(__file__).parent.resolve()
    data_path = current_file_dir.joinpath("../../../examples/ai_spleen_seg_data/dcm")

    loader = DICOMDataLoaderOperator()
    series_selector = DICOMSeriesSelectorOperator()
    dcm_to_volume_op = DICOMSeriesToVolumeOperator()
    stl_writer = STLConversionOperator()

    # Testing with the main entry functions
    study_list = loader.load_data_to_studies(data_path.absolute())
    study_selected_series_list = series_selector.filter(None, study_list)
    image = dcm_to_volume_op.convert_to_image(study_selected_series_list)
    stl_writer._convert(image, Path("stl/test.stl"))


if __name__ == "__main__":
    test()
