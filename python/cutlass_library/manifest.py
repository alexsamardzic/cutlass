#################################################################################################
#
# Copyright (c) 2017 - 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
# list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
#################################################################################################

"""
Utilities for filtering CUTLASS library kernels and emitting library intitialization
and building code
"""

import enum
import logging
import os.path
import shutil

try:
  import builtins
  if hasattr(builtins, "CUTLASS_IGNORE_PACKAGE") and CUTLASS_IGNORE_PACKAGE == True:
    raise ImportError("Disabling attempt to import cutlass_library")
  from cutlass_library.library import *
  from cutlass_library.gemm_operation import *
  from cutlass_library.rank_k_operation import *
  from cutlass_library.rank_2k_operation import *
  from cutlass_library.trmm_operation import *
  from cutlass_library.symm_operation import *
  from cutlass_library.conv2d_operation import *
  from cutlass_library.conv3d_operation import *
except ImportError:
  from library import *
  from gemm_operation import *
  from rank_k_operation import *
  from rank_2k_operation import *
  from trmm_operation import *
  from symm_operation import *
  from conv2d_operation import *
  from conv3d_operation import *

###################################################################################################
_LOGGER = logging.getLogger(__name__)


class EmitOperationKindAll:
  """
  Emit the OperationKind-level CUTLASS library initialization code.
  The code is generated in the {generated_path}/{operation_kind} directory
  (e.g., tools/library/generated/gemm in the build directory,
  for OperationKind=Gemm), in the all_{operation_kind}_operations.cu file
  (e.g., all_gemm_operations.cu for OperationKind=Gemm).
  That file declares several functions in namespace cutlass::library.
  The functions all have this form,

  void initialize_{configuration_name}(Manifest& manifest);

  The file also _defines_ the following function in that namespace.

  void initialize_all_{operation_kind}_operations(Manifest& manifest);

  That function calls all of the functions declared in this file.
  Those functions are defined in subdirectories
  (which this class does not create).
  """

  def __init__(self, generated_path, kind, args):
    self.generated_path = generated_path
    self.kind = kind
    self.args = args

    self.header_template ="""
/*
 Generated by manifest.py - Do not edit.
*/

#include "cutlass/cutlass.h"
#include "cutlass/library/library.h"
#include "cutlass/library/manifest.h"

namespace cutlass {
namespace library {

///////////////////////////////////////////////////////////////////////////////////////////////////

"""

    self.entry_template = """

//
// Entry point to construct operations
//
void initialize_all_${operation_name}_operations(Manifest &manifest) {
"""
    self.configuration_prototype_template = "void initialize_${configuration_name}(Manifest &manifest);\n"
    self.configuration_template ="  initialize_${configuration_name}(manifest);\n"

    self.epilogue_template ="""}

///////////////////////////////////////////////////////////////////////////////////////////////////

} // namespace library
} // namespace cutlass

"""

  #
  def __enter__(self):
    _LOGGER.debug("*** EmitOperationKindAll::__enter__")

    self.operation_path = os.path.join(self.generated_path, OperationKindNames[self.kind])
    _LOGGER.debug('***   operation_path (directory to create): ' +
                  str(self.operation_path));
    os.makedirs(self.operation_path, exist_ok=True)

    self.top_level_path = os.path.join(self.operation_path, f"all_{OperationKindNames[self.kind]}_operations.cu")
    _LOGGER.debug(f"***   top_level_path (file to write): {str(self.top_level_path)}")

    self.top_level_file = open(self.top_level_path, "w")
    self.top_level_file.write(self.header_template)

    self.source_files = [self.top_level_path,]

    self.configurations = []

    return self

  #
  def emit(self, operations):
    _LOGGER.debug('*** EmitOperationKindAll::emit')
    _LOGGER.debug(f"***   len(operations): {len(operations)}")
    _LOGGER.debug(f"***   min_cc list: {sorted(min_cc for min_cc, _ in operations.items())}")

    for min_cc, configurations in sorted(operations.items()):
      _LOGGER.debug(f"***   min_cc={min_cc}")

      for configuration_name, _ in configurations.items():
        _LOGGER.debug(f"***     configuration_name={configuration_name}")
        self.configurations.append(configuration_name)
        self.top_level_file.write(SubstituteTemplate(self.configuration_prototype_template, {'configuration_name': configuration_name} ))

  #
  def __exit__(self, exception_type, exception_value, traceback):
    _LOGGER.debug("*** EmitOperationKindAll::__exit__")

    self.top_level_file.write(SubstituteTemplate(self.entry_template, {'operation_name': OperationKindNames[self.kind]}))

    for configuration_name in self.configurations:
      self.top_level_file.write(SubstituteTemplate(self.configuration_template, {'configuration_name': configuration_name}))

    self.top_level_file.write(self.epilogue_template)
    self.top_level_file.close()


class EmitOperationKindLibrary:
  """
  Emit the CUTLASS library initialization code for each OperationKind.
  The code is generated in the directory
  {generated_path}/{operation_kind}/{min_cc}
  (e.g., tools/library/generated/gemm/90 in the build directory,
  for min_cc=90 and OperationKind=Gemm), in the file
  all_sm{min_cc}_{operation_kind}_operations.cu
  (e.g., all_sm90_gemm_operations.cu for min_cc=90 and OperationKind=Gemm).
  The min_cc variable here indicates the minimum GPU architecture version
  that the things to be initialized require.
  For example, min_cc=90 indicates sm90.

  That file declares several functions in namespace cutlass::library.
  The functions all have this form,

  void initialize_all_sm{min_cc}_{subclass_name}_{extended_name}_operations(Manifest& manifest);

  where extended_name is operation.extended_name() for all the operations
  given to the emit method (which see below).  (All operations for a given
  configuration_name are guaranteed to have the same extended_name().)

  The file also _defines_ the following function in that namespace.

  void initialize_all_sm{min_cc}__{operation_kind}_operations(Manifest& manifest);

  That function calls all of the functions declared in this file.
  Those functions are defined in subdirectories.
  The mapping from OperationKind to emitter handles the details
  of what happens in each of those subdirectories.
  """

  def __init__(self, generated_path, min_cc, kind, args):
    self.generated_path = generated_path
    self.min_cc = min_cc
    self.kind = kind
    self.args = args
    self.emitters = {
      OperationKind.Gemm: EmitGemmConfigurationLibrary,
      OperationKind.Conv2d: EmitConv2dConfigurationLibrary,
      OperationKind.Conv3d: EmitConv3dConfigurationLibrary,
      OperationKind.RankK: EmitRankKConfigurationLibrary,
      OperationKind.Rank2K: EmitRank2KConfigurationLibrary,
      OperationKind.Trmm: EmitTrmmConfigurationLibrary,
      OperationKind.Symm: EmitSymmConfigurationLibrary
    }

    self.header_template ="""
/*
 Generated by manifest.py - Do not edit.
*/

#include "cutlass/cutlass.h"
#include "cutlass/library/library.h"
#include "cutlass/library/manifest.h"

namespace cutlass {
namespace library {

///////////////////////////////////////////////////////////////////////////////////////////////////

"""
    self.entry_template = """

//
// Entry point to construct operations
//
void initialize_all_sm${min_cc}_${subclass_name}_${operation_name}_operations(Manifest &manifest) {
"""
    self.configuration_prototype_template = "void initialize_${configuration_name}(Manifest &manifest);\n"
    self.configuration_template = "  initialize_${configuration_name}(manifest);\n"
    self.subclass_call_template = "  initialize_all_sm${min_cc}_${subclass_name}_${operation_name}_operations(manifest);\n"
    self.subclass_prototype_template = "void initialize_all_sm${min_cc}_${subclass_name}_${operation_name}_operations(Manifest &manifest);\n"
    self.epilogue_template ="""}

///////////////////////////////////////////////////////////////////////////////////////////////////

} // namespace library
} // namespace cutlass

"""

  #
  def __enter__(self):
    _LOGGER.debug("*** EmitOperationKindLibrary::__enter__")
    _LOGGER.debug(f"***   generated_path: {str(self.generated_path)}")
    _LOGGER.debug(f"***   OperationKindNames[kind]: {OperationKindNames[self.kind]}")
    _LOGGER.debug(f"***   min_cc: {self.min_cc}")

    self.operation_path = os.path.join(self.generated_path, OperationKindNames[self.kind], str(self.min_cc))
    _LOGGER.debug(f"***   operation_path (directory to make): {str(self.operation_path)}")
    os.makedirs(self.operation_path)

    self.top_level_path = os.path.join(self.operation_path, f"all_sm{self.min_cc}_{OperationKindNames[self.kind]}_operations.cu")
    _LOGGER.debug(f"***   top_level_path (file to write): {str(self.top_level_path)}")

    self.top_level_file = open(self.top_level_path, "w")
    self.top_level_file.write(self.header_template)

    self.source_files = {}

    # Each {operation_kind x cc} combination is further decomposed by the instruction
    # types used. This dictionary used to track the file handles for the top-level
    # files of each subclass
    self.subclass_files = {}

    # Configurations in each sub class
    self.subclass_configurations = {}

    return self

  #
  def emit(self, configuration_name, operations):
    _LOGGER.debug("*** EmitOperationKindLibrary::emit")
    _LOGGER.debug(f"***   configuration_name: {configuration_name}")

    assert len(operations) > 0

    # The extended name for all operations of a given configuration_name is guaranteed
    # to be the same because extended_name() is used in defining configuration_name. Thus,
    # we can safely use the extended_name() of the first operation.
    extended_name = operations[0].extended_name()
    _LOGGER.debug('***   extended_name (for all ops): ' + extended_name)

    # Create a directory for operations with this subclass if it does not exist
    if extended_name not in self.subclass_files:
      subclass_path = os.path.join(self.operation_path, extended_name)
      _LOGGER.debug(f"***     subclass_path: {str(subclass_path)}")
      os.mkdir(subclass_path)

      self.subclass_configurations[extended_name] = []

      # Open a new top-level file for this sub class
      subclass_top_level_path = os.path.join(
        subclass_path, f"all_sm{self.min_cc}_{extended_name}_{OperationKindNames[self.kind]}_operations.cu")
      _LOGGER.debug('***     subclass_top_level_path (min_cc, extended_name, ' +
                    'OperationKind): ' + str(subclass_top_level_path))

      self.subclass_files[extended_name] = open(subclass_top_level_path, "w")
      self.subclass_files[extended_name].write(self.header_template)

      self.source_files[extended_name] = [subclass_top_level_path]

    subclass_dir = os.path.dirname(self.subclass_files[extended_name].name)
    _LOGGER.debug('***   subclass_dir: ' + str(subclass_dir))

    with self.emitters[self.kind](subclass_dir, configuration_name) as configuration_emitter:
      for operation in operations:
        configuration_emitter.emit(operation)

      _LOGGER.debug('***   configuration_emitter.configuration_path: ' +
                    str(configuration_emitter.configuration_path))
      self.source_files[extended_name].append(configuration_emitter.configuration_path)

    self.subclass_configurations[extended_name].append(configuration_name)
    self.subclass_files[extended_name].write(SubstituteTemplate(self.configuration_prototype_template, {'configuration_name': configuration_name} ))

  #
  def __exit__(self, exception_type, exception_value, traceback):
    _LOGGER.debug("*** EmitOperationKindLibrary::__exit__")    
    for subclass_name, subclass_file in sorted(self.subclass_files.items()):
      subclass_cfg = {
        'min_cc': str(self.min_cc),
        'subclass_name': subclass_name,
        'operation_name': OperationKindNames[self.kind]
      }
      self.top_level_file.write(SubstituteTemplate(self.subclass_prototype_template, subclass_cfg))

    self.top_level_file.write(
      SubstituteTemplate(self.entry_template, {
        'min_cc': str(self.min_cc),
        'subclass_name': '',
        'operation_name': OperationKindNames[self.kind]
      }))

    # Finish and close all subclass files
    for subclass_name, subclass_file in sorted(self.subclass_files.items()):
      subclass_cfg = {
        'min_cc': str(self.min_cc),
        'subclass_name': subclass_name,
        'operation_name': OperationKindNames[self.kind]
      }
      subclass_file.write(SubstituteTemplate(self.entry_template, subclass_cfg))

      for configuration in self.subclass_configurations[subclass_name]:
        subclass_file.write(
          SubstituteTemplate(self.configuration_template, {
            'configuration_name': configuration
          }))

      subclass_file.write(self.epilogue_template)
      subclass_file.close()

      # Write the call to initialize_all for this subclass to the top-level file
      self.top_level_file.write(SubstituteTemplate(self.subclass_call_template, subclass_cfg))

    self.top_level_file.write(self.epilogue_template)
    self.top_level_file.close()

class EmitInterfaceLibrary:
  """
  Emit the topmost-level CUTLASS library initialization code.
  The code is generated in the generated_path directory
  (e.g., tools/library/generated in the build directory),
  in the initialize_all.cpp file.
  That file declares several functions in namespace cutlass::library.
  The functions all have this form,

  void initialize_all_{operation_kind}_operations(Manifest& manifest);

  where {operation_kind} abbreviates the "kind" of operation
  (e.g., gemm for matrix-matrix multiply, conv2d for 2-d convolution,
  or trmm for triangular solve with multiple right-hand sides).
  The definitions of these functions live in subdirectories.

  The file also _defines_ the following function in that namespace.

  void initialize_all(Manifest& manifest);

  That function first prepares the manifest, and then
  calls all of the functions declared in this file.
  """

  def __init__(self, generated_path, operation_count, args):
    self.generated_path = generated_path
    self.args = args

    self.prototypes = []
    self.fn_calls = []
    self.operation_count = str(operation_count)

    self.top_level_hdr_template = '''
/*
 Generated by manifest.py - Do not edit.
*/
'''
    self.top_level_prologue = '''

#include "cutlass/library/library.h"
#include "cutlass/library/manifest.h"

namespace cutlass {
\tnamespace library {

${prototypes}
'''

    self.top_level_initialize_kind = '''
\t\tvoid initialize_all_${kind}_operations(Manifest &manifest) {
${fn_calls}
\t\t}
'''

    self.top_level_initialize = '''
\t\tvoid initialize_all(Manifest &manifest) {
\t\t\tmanifest.reserve(${operation_count});\n
${fn_calls}
\t\t}
'''

    self.top_level_suffix = '''
\t} // namespace library
} // namespace cutlass

'''

  #
  def __enter__(self):
    _LOGGER.debug("*** EmitInterfaceLibrary::__enter__")

    self.top_level_path = os.path.join(self.generated_path, 'initialize_all.cpp')
    _LOGGER.debug("***   top_level_path: " + str(self.top_level_path))

    self.top_level_file = open(self.top_level_path, "w")
    self.top_level_file.write(self.top_level_hdr_template)

    self.source_files = [self.top_level_path,]

    return self

  #
  def emit(self, operation_name):
    _LOGGER.debug("*** EmitInterfaceLibrary::emit")
    _LOGGER.debug("***   operation_name: " + operation_name)

    self.prototypes.append(SubstituteTemplate(
       "\t\tvoid initialize_all_${operation_kind}_operations(Manifest &manifest);",
       {'operation_kind': operation_name}))

    self.fn_calls.append(SubstituteTemplate(
      "\t\t\tinitialize_all_${operation_kind}_operations(manifest);",
      {'operation_kind': operation_name}))

  #
  def __exit__(self, exception_type, exception_value, traceback):
    _LOGGER.debug("*** EmitInterfaceLibrary::__exit__")

    self.top_level_file.write(SubstituteTemplate(self.top_level_prologue, {'prototypes':"\n".join(self.prototypes)}))

    # Write out initialize_all method
    self.top_level_file.write(SubstituteTemplate(self.top_level_initialize,
                              {'operation_count': self.operation_count, 'fn_calls':"\n".join(self.fn_calls)}))

    self.top_level_file.write(self.top_level_suffix)
    self.top_level_file.close()

###################################################################################################
###################################################################################################

class Options:
  def __init__(self):
    pass

###################################################################################################

#
class Manifest:

  #
  def __init__(self, args = None):
    self.operations = {}
    self.args = args
    self.operation_count = 0
    self.operations_by_name = {}

    self.kernel_filter = ''
    self.kernel_filter_list = []
    self.kernel_names = []
    self.operations_enabled = []
    self.selected_kernels = []
    self.ignore_kernel_names = []
    self.exclude_kernel_names = []
    self.compute_capabilities_baseline = [50,]
    self.compute_capabilities_feature_set = ['50',]
    self.curr_build_dir = '.'
    self.filter_by_cc = True

    if self.args:
      self.kernel_filter = self.args.kernels
      self.curr_build_dir = args.curr_build_dir

      # A common user error is to use commas instead of semicolons.
      if ',' in args.architectures:
        raise RuntimeError("The list of architectures (CMake option CUTLASS_NVCC_ARCHS) must be semicolon-delimited.\nDon't use commas to separate the architectures; use semicolons.\nYou specified the list as: " + args.architectures)
      
      self.compute_capabilities_feature_set = args.architectures.split(';') if len(args.architectures) else ['50',]
      self.compute_capabilities_baseline = sorted(set(int(arch.split('a')[0].split('f')[0]) for arch in self.compute_capabilities_feature_set))

      if args.filter_by_cc in ['false', 'False', '0']:
        self.filter_by_cc = False

    if args.operations == 'all':
      self.operations_enabled = []
    else:
      operations_list = [
        OperationKind.Gemm
        , OperationKind.Conv2d
        , OperationKind.Conv3d
          , OperationKind.RankK
          , OperationKind.Trmm
          , OperationKind.Symm
      ]
      self.operations_enabled = [x for x in operations_list if OperationKindNames[x] in args.operations.split(',')]

    if args.kernels == 'all':
      self.kernel_names = []
    else:
      self.kernel_names = [x for x in args.kernels.split(',') if x != '']

    self.ignore_kernel_names = [x for x in args.ignore_kernels.split(',') if x != '']
    self.exclude_kernel_names = [x for x in args.exclude_kernels.split(',') if x != '']

    if args.kernel_filter_file is None:
        self.kernel_filter_list = []
    else:
        self.kernel_filter_list = self.get_kernel_filters(args.kernel_filter_file)
        _LOGGER.debug("Using {filter_count} kernel filters from {filter_file}".format(
            filter_count = len(self.kernel_filter_list),
            filter_file = args.kernel_filter_file))

    self.operation_count = 0
    self.operations_by_name = {}
    self.disable_full_archs_compilation = args.disable_full_archs_compilation
    self.is_kernel_filter_set_to_all = args.instantiation_level == "max" and args.kernels != ''
    self.instantiation_level = 0
    try:
        self.instantiation_level = int(args.instantiation_level)
    except ValueError:
        self.instantiation_level = 0

  def get_sm90_instantiation_level(self, pruned_level=0, default_level=111, exhaustive_level=9992):
    # Non-negative integer which determines how many kernels are instantiated.
    # 0 = 0000 generates the fewest kernels, 9999 generates all possible combinations.
    # increasing first digit reduces schedule / mixed type pruning,
    # increasing second digit generates more cluster sizes,
    # increasing third digit generates more MMA multipliers,
    # increasing fourth digit generates more instruction shapes.

    if self.instantiation_level > 0:
        return self.instantiation_level

    elif self.is_kernel_filter_set_to_all:
        return exhaustive_level

    elif self.kernel_filter == '':
        return pruned_level

    else:
        return default_level


  def get_kernel_filters(self, kernelListFile):
    if os.path.isfile(kernelListFile):
        with open(kernelListFile, 'r') as fileReader:
            lines = [line.rstrip() for line in fileReader if not line.startswith("#")]

        lines = [re.compile(line) for line in lines if line]
        return lines
    else:
        return []

  #
  def filter_out_kernels(self, kernel_name, kernel_filter_list):

    for kernel_filter_re in kernel_filter_list:
        if kernel_filter_re.search(kernel_name) is not None:
            return True

    return False


  #
  def _filter_string_matches(self, filter_string, haystack):
    ''' Returns true if all substrings appear in the haystack in order'''
    substrings = filter_string.split('*')
    for sub in substrings:
      idx = haystack.find(sub)
      if idx < 0:
        return False
      haystack = haystack[idx + len(sub):]
    return True

  #
  def filter(self, operation):
    ''' Filtering operations based on various criteria'''

    # filter based on compute capability
    enabled = not (self.filter_by_cc)

    for cc in self.compute_capabilities_baseline:

      if cc >= operation.tile_description.minimum_compute_capability and \
         cc <= operation.tile_description.maximum_compute_capability and \
         (cc not in SharedMemPerCC or SharedMemPerCC[cc] >= CalculateSmemUsage(operation)):

        enabled = True
        break

    if not enabled:
      return False

    if len(self.operations_enabled) and not operation.operation_kind in self.operations_enabled:
      return False

    name = operation.procedural_name()

    # eliminate duplicates
    if name in self.operations_by_name.keys():
      return False

    # Filter based on list of valid substrings
    if len(self.kernel_names):
      enabled = False

      # compare against the include list
      for name_substr in self.kernel_names:
        if self._filter_string_matches(name_substr, name):
          _LOGGER.debug(f"Kernel {name} included due to filter string '{name_substr}'.")
          enabled = True
          break
        else:
          _LOGGER.debug(f"Kernel {name} NOT included due to not matching '{name_substr}'.")

      # compare against the exclude list
      for name_substr in self.ignore_kernel_names:
        if self._filter_string_matches(name_substr, name):
          _LOGGER.debug(f"Kernel {name} ignored due to filter string '{name_substr}'.")
          enabled = False
          break
        else:
          _LOGGER.debug(f"Kernel {name} NOT ignored due to not matching '{name_substr}'.")

    if len(self.kernel_filter_list) > 0:
      if self.filter_out_kernels(name, self.kernel_filter_list):
        _LOGGER.debug(f"Kernel {name} matched via kernel filter file.")
        enabled = True
      else:
        _LOGGER.debug(f"Kernel {name} culled due to no match in kernel filter file.")
        enabled = False

    # CUTLASS_LIBRARY_IGNORE_KERNELS ("ignore" list) only takes effect
    # if CUTLASS_LIBRARY_KERNELS was specified.
    # Changing that would break backwards compatibility.
    # Thus, CUTLASS has introduced the new CMake option CUTLASS_LIBRARY_EXCLUDE_KERNELS,
    # that always takes effect, whether or not CUTLASS_LIBRARY_KERNELS was specified.
    for name_substr in self.exclude_kernel_names:
      if self._filter_string_matches(name_substr, name):
        _LOGGER.debug(f"Kernel {name} excluded due to filter string '{name_substr}'.")
        enabled = False
        break
      else:
        _LOGGER.debug(f"Kernel {name} NOT excluded due to not matching '{name_substr}'.")

    # TODO: filter based on compute data type
    return enabled
  #

  #
  def append(self, operation):
    '''
      Inserts the operation.

      operation_kind -> configuration_name -> []
    '''

    if self.filter(operation):

      self.selected_kernels.append(operation.procedural_name())

      self.operations_by_name[operation.procedural_name()] = operation

      # add the configuration
      configuration_name = operation.configuration_name()

      # Split operations by minimum CC
      min_cc = operation.arch

      if operation.operation_kind not in self.operations.keys():
        self.operations[operation.operation_kind] = {}

      if min_cc not in self.operations[operation.operation_kind]:
        self.operations[operation.operation_kind][min_cc] = {}

      if configuration_name not in self.operations[operation.operation_kind][min_cc].keys():
        self.operations[operation.operation_kind][min_cc][configuration_name] = []

      self.operations[operation.operation_kind][min_cc][configuration_name].append(operation)
      self.operation_count += 1
    else:
      _LOGGER.debug("Culled {} from manifest".format(operation.procedural_name()))
  #

  def emit_manifest_cmake(self, manifest_path, top_level_path, source_files):
    with open(manifest_path, "w") as manifest_file:

      target_text = SubstituteTemplate("""cutlass_target_sources(cutlass_library_objs PRIVATE
      """, { })
      manifest_file.write(target_text + '\n\n')
      manifest_file.write("    %s\n" % str(top_level_path.replace('\\', '/')))
      generated_path = os.path.join(self.curr_build_dir, 'generated')
      for kind in self.operations.keys():
        kind_str = OperationKindNames[kind]
        all_kind_file = os.path.join(generated_path, kind_str, f"all_{kind_str}_operations.cu").replace('\\', '/')
        manifest_file.write(f"    {all_kind_file}\n")
      manifest_file.write(')\n\n')

      for kind in self.operations.keys():
        for min_cc in sorted(self.operations[kind].keys()):
          for subclass in sorted(source_files[kind][min_cc].keys()):
            target_text = SubstituteTemplate("""cutlass_add_cutlass_library(
      SUFFIX ${kind}_sm${min_cc}_${subclass}
""", { 'min_cc': str(min_cc), 'kind': OperationKindNames[kind], 'subclass': subclass })
            manifest_file.write(target_text + '\n\n')

            for source_file in source_files[kind][min_cc][subclass]:
              manifest_file.write("    %s\n" % str(source_file.replace('\\', '/')))

            manifest_file.write(")\n")

          if self.disable_full_archs_compilation:
            self.emit_disable_full_archs_compilation(manifest_file, source_files)

  def emit_disable_full_archs_compilation(manifest_file, source_files):
      def for_hopper(name):
          pass

      def for_ampere(name):
          return "16816" in name or \
                  "16832" in name or \
                  "16864" in name or \
                  ("1688" in name and "tf32" in name)

      def for_turing(name):
          return ("1688" in name and "tf32" not in name) or \
                  "8816" in name

      def for_volta(name):
          return "884" in name

      def is_cpp(name):
          return name.endswith(".cpp")

      def get_src_archs_str_given_requested_cuda_archs(archs, source_file):
          intersected_archs = archs & set(self.compute_capabilities_baseline)
          if intersected_archs == set():
              raise RuntimeError(
                    """
                    Empty archs set for file {} after taking
                    the intersection of {} (global requested archs) and
                    {} (per file requested archs)
                    """.format(source_file, set(self.compute_capabilities_baseline), archs))
          else:
              return " ".join(map(str, intersected_archs))

      for min_cc in sorted(source_files.keys()):
        for source_file in source_files[min_cc]:
            if is_cpp(source_file):
                continue # skip because source is cpp
            elif for_ampere(source_file):
                archs_str = get_src_archs_str_given_requested_cuda_archs({80, 87, 90}, source_file)
            elif for_turing(source_file):
                archs_str = get_src_archs_str_given_requested_cuda_archs({75}, source_file)
            elif for_volta(source_file):
                archs_str = get_src_archs_str_given_requested_cuda_archs({70, 72}, source_file)
            else:
                raise RuntimeError("Per file archs are not set {}, as there is no rule specified for this file pattern".format(source_file))

            manifest_file.write("cutlass_apply_cuda_gencode_flags({} SM_ARCHS {})\n".format(str(source_file.replace('\\', '/')), archs_str))

  #
  def emit(self, target = GeneratorTarget.Library):

    operation_emitters = {
      GeneratorTarget.Library: EmitOperationKindLibrary
    }

    # Emitters for all operations that fall under a particular kind (e.g., GEMM, Conv2d)
    kind_emitters = {
      GeneratorTarget.Library: EmitOperationKindAll
    }

    interface_emitters = {
      GeneratorTarget.Library: EmitInterfaceLibrary
    }

    generated_path = os.path.join(self.curr_build_dir, 'generated')

    # create generated/
    if os.path.exists(generated_path):
      shutil.rmtree(generated_path)

    os.mkdir(generated_path)

    with interface_emitters[target](generated_path, self.operation_count, self.args) as iface_emitter:
      top_level_path = iface_emitter.top_level_path
      for operation_kind in self.operations.keys():
        iface_emitter.emit(OperationKindNames[operation_kind])

    source_files = {}
    for kind in self.operations.keys():
      source_files[kind] = {}
      for min_cc in self.operations[kind].keys():
        source_files[kind][min_cc] = {}

    for operation_kind, ops in self.operations.items():
      for min_cc, configurations in sorted(ops.items()):
        with operation_emitters[target](generated_path, min_cc, operation_kind, self.args) as operation_kind_emitter:
          for configuration_name, operations in configurations.items():
            _LOGGER.info(f"Emitting {configuration_name} with {len(operations)} operation{'' if len(operations) == 1 else 's'}.")
            operation_kind_emitter.emit(configuration_name, operations)

          for subclass, files in operation_kind_emitter.source_files.items():
            if subclass not in source_files[operation_kind][min_cc]:
              source_files[operation_kind][min_cc][subclass] = []
            source_files[operation_kind][min_cc][subclass].extend(operation_kind_emitter.source_files[subclass])

      # Emit top level all_{gemm, conv2d, ...}_operations.cu files
      with kind_emitters[target](generated_path, operation_kind, self.args) as operation_kind_emitter:
        operation_kind_emitter.emit(ops)

    # write the manifest.cmake file containing paths from all targets
    manifest_path = os.path.join(generated_path, "manifest.cmake")

    self.emit_manifest_cmake(manifest_path, top_level_path, source_files)

###################################################################################################
